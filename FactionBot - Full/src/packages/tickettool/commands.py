# -*- coding: utf-8 -*-
'''
TicketTool.commands — registers all Tier 1 prefix commands on the bot.

Called once from Bot.py after `bot` is defined:
    TicketTool.commands.register(bot)

All commands are registered programmatically (the decorator form works inside
a function because `bot.command` returns a decorator we can apply).
Every command is permission-gated (manage_channels / admin) and ephemeral-by-
default for the config commands so they don't spam the ticket channel.

Commands added:
  !naming / !naming view         — set/view naming templates + padding
  !schedule / !schedule view      — per-panel business hours
  !claimconfig / !claimconfig view — advanced claiming config
  !roleauto / !roleauto view     — open/close/claim role automation
  !automate / !automate list      — automation engine CRUD
  !escalate                       — escalate current ticket
  !escalateroute / !escalateroute list — escalation route config
  !transcriptconfig / !view        — advanced transcript config
  !slaconfig / !slareport         — SLA config + report
  !analytics                      — ticket analytics overview
  !csat                           — CSAT report (by staff/panel/time)
  !staffstats                     — per-staff performance
  !export                         — CSV export of tickets
'''

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import (
    naming as naming_mod, scheduling as sched_mod, claiming as claim_mod,
    role_automation as role_mod, automations as auto_mod, escalation as esc_mod,
    transcripts as tr_mod, sla as sla_mod, analytics as an_mod,
    # Tier 2
    kb as kb_mod, thread_tickets as tt_mod, staff_threads as st_mod,
    channel_recycle as cr_mod, i18n as i18n_mod, branded_replies as br_mod,
    flows as flow_mod, custom_commands as cc_mod,
    # Tier 3
    multi_embed as me_mod, moderator_messages as mm_mod,
    flow_reviews as fr_mod,
    # Ticket Tool feature-parity
    canned as canned_mod,
)
from .db import _json_loads_list


def register(bot):
    '''Register every premium prefix command on `bot`.'''

    def _pdb():
        return getattr(bot, 'premium_db', None)

    def _require_ticket(ctx) -> Optional[dict]:
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            return None
        return tt.data_manager.load_ticket_by_channel(ctx.channel.id)

    def _require_panel(ctx, ticket):
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None or not ticket:
            return None
        pid = ticket.get('panel_id')
        return tt.data_manager.load_ticket_panel(pid) if pid else None

    def _parse_id(raw, *, mention_prefix: str = '<@&') -> Optional[int]:
        '''Parse an optional role/channel/user ID (or mention) into an int.

        Raises ValueError for non-numeric input so callers can respond with a
        clear ephemeral error instead of a traceback.
        '''
        if raw is None:
            return None
        return int(str(raw).strip().lstrip(mention_prefix).rstrip('>'))

    # =================================================================
    # NAMING (Feature 5 + 8)
    # =================================================================
    @bot.command(name="naming", description="Configure ticket naming templates + number padding")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID (use !panels to list)",
        open_template="Open-ticket name template, e.g. support-{ticket.count}-{ticket.user}",
        closed_template="Closed-ticket name template (optional)",
        claimed_template="Claimed-ticket name template (optional)",
        padding="Zero-pad the ticket count to this many digits (0-20, e.g. 4 -> #0057)",
    )
    async def naming_cmd(ctx: commands.Context, panel_id: str,
                          open_template: Optional[str] = None,
                          closed_template: Optional[str] = None,
                          claimed_template: Optional[str] = None,
                          padding: Optional[int] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium system not initialized.", ephemeral=True)
            return
        panel = bot.ticket_tool.data_manager.load_ticket_panel(panel_id) if hasattr(bot, 'ticket_tool') else None
        if not panel:
            await ctx.send(f"Panel `{panel_id}` not found.", ephemeral=True)
            return
        existing = pdb.get_naming(panel_id) or {}
        cfg = {
            'panel_id': panel_id,
            'guild_id': ctx.guild.id,
            'open_template': open_template if open_template is not None else existing.get('open_template'),
            'closed_template': closed_template if closed_template is not None else existing.get('closed_template'),
            'claimed_template': claimed_template if claimed_template is not None else existing.get('claimed_template'),
            'number_padding': int(padding) if padding is not None else int(existing.get('number_padding') or 0),
        }
        if int(cfg['number_padding']) < 0 or int(cfg['number_padding']) > 20:
            await ctx.send("Padding must be between 0 and 20.", ephemeral=True)
            return
        pdb.upsert_naming(cfg)
        embed = discord.Embed(title="🏷️ Ticket Naming Configured", color=discord.Color.green())
        embed.add_field(name="Panel", value=f"`{panel.get('name')}` ({panel_id})", inline=False)
        embed.add_field(name="Open template", value=f"`{cfg['open_template'] or '(default)'}`", inline=False)
        embed.add_field(name="Closed template", value=f"`{cfg['closed_template'] or '(none)'}`", inline=False)
        embed.add_field(name="Claimed template", value=f"`{cfg['claimed_template'] or '(none)'}`", inline=False)
        embed.add_field(name="Number padding", value=str(cfg['number_padding']), inline=True)
        embed.add_field(name="Variables", value="{ticket.id} {ticket.count} {ticket.user} {claim.user} {panel.name} |lower |upper |pad:4 |truncate:20", inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # SCHEDULING (Feature 4)
    # =================================================================
    @bot.command(name="schedule", description="Configure per-panel business hours")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        timezone="Timezone name (UTC, ET, PT, GMT, CET, JST, AEST, IST, ...)",
        periods='JSON list, e.g. [{"day":"weekday","start":"16:00","end":"22:00"}]',
        unavailable_message="Message shown when panel is closed",
        bypass_roles="Comma-separated role IDs that bypass the schedule",
        enabled="Enable (true) or disable (false) the schedule",
    )
    async def schedule_cmd(ctx: commands.Context, panel_id: str,
                            timezone: str = "UTC",
                            periods: str = "[]",
                            unavailable_message: Optional[str] = None,
                            bypass_roles: Optional[str] = None,
                            enabled: bool = True) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        try:
            period_list = json.loads(periods) if periods else []
        except json.JSONDecodeError as e:
            await ctx.send(f"Invalid periods JSON: {e}", ephemeral=True); return
        # Validate each period.
        for p in period_list:
            norm = sched_mod.normalize_period(p)
            if not norm:
                await ctx.send(f"Invalid period: {p}", ephemeral=True); return
        bypass_ids = []
        if bypass_roles:
            for tok in bypass_roles.replace(',', ' ').split():
                tok = tok.strip().lstrip('<@&').rstrip('>')
                if tok.isdigit():
                    bypass_ids.append(int(tok))
        cfg = {
            'panel_id': panel_id, 'guild_id': ctx.guild.id,
            'timezone': timezone, 'periods': period_list,
            'unavailable_message': unavailable_message,
            'bypass_role_ids': bypass_ids, 'enabled': enabled,
        }
        sched_mod.save_config(pdb, panel_id, ctx.guild.id,
                              timezone=timezone, periods=period_list,
                              unavailable_message=unavailable_message,
                              bypass_role_ids=bypass_ids, enabled=enabled)
        # Pretty summary.
        embed = discord.Embed(title="🕘 Panel Schedule", color=discord.Color.green())
        embed.add_field(name="Panel", value=panel_id, inline=False)
        embed.add_field(name="Timezone", value=timezone, inline=True)
        embed.add_field(name="Enabled", value="✅" if enabled else "❌", inline=True)
        if period_list:
            lines = [f"• {p.get('day')}: {p.get('start')}-{p.get('end')}" for p in period_list]
            embed.add_field(name="Periods", value="\n".join(lines), inline=False)
        if bypass_ids:
            embed.add_field(name="Bypass roles", value=", ".join(f"<@&{r}>" for r in bypass_ids), inline=False)
        open_now, msg = sched_mod.is_panel_open_now(pdb, {'panel_id': panel_id}, None)
        embed.add_field(name="Open now?", value="✅ Yes" if open_now else "❌ No", inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="schedview", description="View a panel's current schedule")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID")
    async def schedview_cmd(ctx: commands.Context, panel_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        cfg = sched_mod.get_config(pdb, panel_id)
        if not cfg:
            await ctx.send("No schedule configured for that panel.", ephemeral=True); return
        embed = discord.Embed(title="🕘 Panel Schedule", color=discord.Color.blurple())
        embed.add_field(name="Panel", value=panel_id, inline=False)
        embed.add_field(name="Timezone", value=cfg.get('timezone','UTC'), inline=True)
        embed.add_field(name="Enabled", value="✅" if cfg.get('enabled') else "❌", inline=True)
        periods = _json_loads_list(cfg.get('periods'))
        if periods:
            lines = [f"• {p.get('day')}: {p.get('start')}-{p.get('end')}" for p in periods]
            embed.add_field(name="Periods", value="\n".join(lines), inline=False)
        bypass = _json_loads_list(cfg.get('bypass_role_ids'))
        if bypass:
            embed.add_field(name="Bypass roles", value=", ".join(f"<@&{r}>" for r in bypass), inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # ADVANCED CLAIMING CONFIG (Feature 2)
    # =================================================================
    @bot.command(name="claimconfig", description="Configure advanced claiming for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        only_claimer_unclaim="Only the claimer can unclaim (true/false)",
        claimer_and_owner_only_actions="Only claimer + owner may manage ticket (true/false)",
        auto_replace_claimer="New claim silently replaces old claim (true/false)",
        allow_owner_claim="Ticket creator may claim their own ticket (true/false)",
        rename_on_claim="Full claimed channel name template (supports variables like {ticket.count}, {user.name})",
        move_category_on_claim="Category ID to move the ticket into on claim",
        hide_from_other_staff="Hide claimed ticket from other staff (true/false)",
        change_support_perms_on_claim="Demote support role to read-only on claim (true/false)",
        claimed_message="Custom message shown when a ticket is claimed",
        unclaimed_message="Custom message shown when a ticket is unclaimed",
    )
    async def claimconfig_cmd(ctx: commands.Context, panel_id: str,
                               only_claimer_unclaim: Optional[bool] = None,
                               claimer_and_owner_only_actions: Optional[bool] = None,
                               auto_replace_claimer: Optional[bool] = None,
                               allow_owner_claim: Optional[bool] = None,
                               rename_on_claim: Optional[str] = None,
                               move_category_on_claim: Optional[str] = None,
                               hide_from_other_staff: Optional[bool] = None,
                               change_support_perms_on_claim: Optional[bool] = None,
                               claimed_message: Optional[str] = None,
                               unclaimed_message: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        existing = claim_mod.get_config(pdb, panel_id)
        updates = {
            'only_claimer_unclaim': int(only_claimer_unclaim) if only_claimer_unclaim is not None else existing.get('only_claimer_unclaim'),
            'claimer_and_owner_only_actions': int(claimer_and_owner_only_actions) if claimer_and_owner_only_actions is not None else existing.get('claimer_and_owner_only_actions'),
            'auto_replace_claimer': int(auto_replace_claimer) if auto_replace_claimer is not None else existing.get('auto_replace_claimer'),
            'allow_owner_claim': int(allow_owner_claim) if allow_owner_claim is not None else existing.get('allow_owner_claim'),
            'rename_on_claim': rename_on_claim if rename_on_claim is not None else existing.get('rename_on_claim'),
            'move_category_on_claim': move_category_on_claim if move_category_on_claim is not None else existing.get('move_category_on_claim'),
            'hide_from_other_staff': int(hide_from_other_staff) if hide_from_other_staff is not None else existing.get('hide_from_other_staff'),
            'change_support_perms_on_claim': int(change_support_perms_on_claim) if change_support_perms_on_claim is not None else existing.get('change_support_perms_on_claim'),
            'claimed_message': claimed_message if claimed_message is not None else existing.get('claimed_message'),
            'unclaimed_message': unclaimed_message if unclaimed_message is not None else existing.get('unclaimed_message'),
        }
        claim_mod.save_config(pdb, panel_id, ctx.guild.id, updates)
        embed = discord.Embed(title="🔒 Advanced Claiming Configured", color=discord.Color.green())
        embed.add_field(name="Panel", value=panel_id, inline=False)
        for k, v in updates.items():
            embed.add_field(name=k, value=str(v)[:200], inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # ROLE AUTOMATION (Feature 3)
    # =================================================================
    @bot.command(name="roleauto", description="Configure open/close/claim role automation")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        event="Which event: open, close, claim, or unclaim",
        add_roles="Comma-separated role IDs to ADD",
        remove_roles="Comma-separated role IDs to REMOVE",
    )
    async def roleauto_cmd(ctx: commands.Context, panel_id: str, event: str,
                            add_roles: Optional[str] = None,
                            remove_roles: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if event not in role_mod._VALID_EVENTS:
            await ctx.send(f"Invalid event. Use one of: {', '.join(role_mod._VALID_EVENTS)}", ephemeral=True); return
        def _parse(s):
            if not s:
                return None
            out = []
            for tok in s.replace(',', ' ').split():
                tok = tok.strip().lstrip('<@&').rstrip('>')
                if tok.isdigit():
                    out.append(int(tok))
            return out
        add_list = _parse(add_roles)
        rem_list = _parse(remove_roles)
        role_mod.set_event_roles(pdb, panel_id, ctx.guild.id, event,
                                  add_roles=add_list, remove_roles=rem_list)
        cfg = role_mod.get_config(pdb, panel_id)
        embed = discord.Embed(title="🎭 Role Automation Configured", color=discord.Color.green())
        embed.add_field(name="Panel", value=panel_id, inline=False)
        embed.add_field(name="Event", value=event, inline=True)
        embed.add_field(name=f"{event} add_roles",
                        value=", ".join(f"<@&{r}>" for r in cfg[f'{event}_add_roles']) or "(none)",
                        inline=False)
        embed.add_field(name=f"{event} remove_roles",
                        value=", ".join(f"<@&{r}>" for r in cfg[f'{event}_remove_roles']) or "(none)",
                        inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # AUTOMATION ENGINE (Feature 1)
    # =================================================================
    @bot.command(name="automate", description="Create or update a ticket automation rule")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        name="A friendly name for this automation",
        trigger="Trigger type: created, closed, reopened, owner_left, close_request, claim, unclaim, delayed, no_response",
        delay_seconds="Delay in seconds (for delayed / no_response triggers)",
        conditions='JSON conditions, e.g. [{"field":"priority","op":"eq","value":"urgent"}]',
        actions='JSON actions, e.g. [{"type":"send_message","channel":"ticket","content":"Hi {ticket.user}!"}] (channel: ticket|transcripts|dm — dm messages the ticket creator)',
        enabled="Enable this automation (true/false)",
        automation_id="Existing automation ID to update (optional)",
    )
    async def automate_cmd(ctx: commands.Context, panel_id: str, name: str, trigger: str,
                            delay_seconds: int = 0,
                            conditions: str = "[]",
                            actions: str = "[]",
                            enabled: bool = True,
                            automation_id: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        try:
            cond_list = json.loads(conditions) if conditions else []
            act_list = json.loads(actions) if actions else []
        except json.JSONDecodeError as e:
            await ctx.send(f"Invalid JSON: {e}", ephemeral=True); return
        try:
            aid = auto_mod.create_or_update(
                pdb, panel_id=panel_id, guild_id=ctx.guild.id, name=name,
                trigger_type=trigger, conditions=cond_list, actions=act_list,
                delay_seconds=delay_seconds, enabled=enabled,
                automation_id=automation_id,
            )
        except ValueError as e:
            await ctx.send(f"Invalid automation: {e}", ephemeral=True); return
        embed = discord.Embed(title="⚙️ Automation Saved", color=discord.Color.green())
        embed.add_field(name="ID", value=f"`{aid}`", inline=True)
        embed.add_field(name="Panel", value=panel_id, inline=True)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="Trigger", value=trigger, inline=True)
        embed.add_field(name="Delay (s)", value=str(delay_seconds), inline=True)
        embed.add_field(name="Enabled", value="✅" if enabled else "❌", inline=True)
        embed.add_field(name="Conditions", value=f"```json\n{json.dumps(cond_list, indent=2)[:1000]}\n```", inline=False)
        embed.add_field(name="Actions", value=f"```json\n{json.dumps(act_list, indent=2)[:1000]}\n```", inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="automatelist", description="List all automations for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID")
    async def automatelist_cmd(ctx: commands.Context, panel_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rows = auto_mod.list_automations(pdb, panel_id)
        if not rows:
            await ctx.send("No automations configured for that panel.", ephemeral=True); return
        embed = discord.Embed(title=f"⚙️ Automations — {panel_id}", color=discord.Color.blurple())
        for r in rows[:15]:
            embed.add_field(
                name=f"`{r['automation_id']}` — {r.get('name','(unnamed)')}",
                value=f"Trigger: {r.get('trigger_type')} • Enabled: {'✅' if r.get('enabled') else '❌'} • Actions: {len(r.get('actions', []))}",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="automatedelete", description="Delete an automation rule")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(automation_id="Automation ID to delete")
    async def automatedelete_cmd(ctx: commands.Context, automation_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if auto_mod.delete(pdb, automation_id):
            await ctx.send(f"Deleted automation `{automation_id}`.", ephemeral=True)
        else:
            await ctx.send(f"Automation `{automation_id}` not found.", ephemeral=True)

    # =================================================================
    # ESCALATION (Feature 10)
    # =================================================================
    @bot.command(name="escalate", description="Escalate the current ticket to another panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        to_panel_id="Target panel ID (optional — uses the configured route if omitted)",
        reason="Reason for escalation",
    )
    async def escalate_cmd(ctx: commands.Context, to_panel_id: Optional[str] = None,
                            reason: str = "No reason provided") -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        ticket = _require_ticket(ctx)
        if not ticket:
            await ctx.send("This is not a ticket channel.", ephemeral=True); return
        panel = _require_panel(ctx, ticket)
        result = await esc_mod.escalate_ticket(
            bot=bot, pdb=pdb, channel=ctx.channel, ticket=ticket,
            guild=ctx.guild, escalated_by=ctx.author,
            to_panel_id=to_panel_id, reason=reason,
        )
        if result['ok']:
            await ctx.send(f"⬆️ Ticket escalated to **{result['to_panel']}**. {reason}", ephemeral=False)
        else:
            await ctx.send(f"Escalation failed: {result['reason']}", ephemeral=True)

    @bot.command(name="escalateroute", description="Configure an escalation route between panels")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        from_panel_id="Source panel ID",
        to_panel_id="Target panel ID",
        notify_role_id="Role to ping on escalation (optional)",
        auto_escalate_hours="Auto-escalate after this many hours (0 = disabled)",
        auto_escalate_priority="Priority to set on auto-escalation (low/normal/high/urgent)",
    )
    async def escalateroute_cmd(ctx: commands.Context, from_panel_id: str, to_panel_id: str,
                                  notify_role_id: Optional[str] = None,
                                  auto_escalate_hours: float = 0.0,
                                  auto_escalate_priority: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rid = None
        try:
            rid = _parse_id(notify_role_id)
        except ValueError:
            await ctx.send("Invalid ID: notify_role_id must be a number.", ephemeral=True); return
        esc_mod.upsert_route(pdb, guild_id=ctx.guild.id,
                              from_panel_id=from_panel_id, to_panel_id=to_panel_id,
                              notify_role_id=rid, auto_escalate_hours=auto_escalate_hours,
                              auto_escalate_priority=auto_escalate_priority)
        await ctx.send(f"✅ Escalation route `{from_panel_id}` → `{to_panel_id}` saved.", ephemeral=True)

    @bot.command(name="escalationhistory", description="View the escalation history of the current ticket")
    @commands.has_permissions(manage_channels=True)
    async def escalationhistory_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        ticket = _require_ticket(ctx)
        if not ticket:
            await ctx.send("This is not a ticket channel.", ephemeral=True); return
        hist = esc_mod.history(pdb, ticket['ticket_id'])
        if not hist:
            await ctx.send("This ticket has never been escalated.", ephemeral=True); return
        embed = discord.Embed(title=f"⬆️ Escalation History — {ticket['ticket_id']}", color=discord.Color.orange())
        for h in hist[:10]:
            ts = (h.get('escalated_at') or '')[:19]
            embed.add_field(
                name=f"{h.get('from_panel_id','?')} → {h.get('to_panel_id','?')}",
                value=f"By <@{h.get('escalated_by') or 'unknown'}> at {ts}\n*{h.get('reason','')}*",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # ADVANCED TRANSCRIPT CONFIG (Feature 6)
    # =================================================================
    @bot.command(name="transcriptconfig", description="Configure advanced transcript automation")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        save_mode="When to save transcripts: on_close, on_delete, both, never",
        auto_dm="Always DM the creator a copy (true/false)",
        custom_message="Custom transcript embed description (supports variables)",
        custom_title="Custom transcript embed title",
        auto_save_channel_id="Archive channel ID for long-term storage",
        enabled="Enable advanced transcript automation (true/false)",
    )
    async def transcriptconfig_cmd(ctx: commands.Context, save_mode: str = "on_close",
                                    auto_dm: bool = False,
                                    custom_message: Optional[str] = None,
                                    custom_title: Optional[str] = None,
                                    auto_save_channel_id: Optional[str] = None,
                                    enabled: bool = True) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if save_mode not in tr_mod.SAVE_MODES:
            await ctx.send(f"Invalid save_mode. Use: {', '.join(tr_mod.SAVE_MODES)}", ephemeral=True); return
        try:
            archive_id = _parse_id(auto_save_channel_id, mention_prefix='<#')
        except ValueError:
            await ctx.send("Invalid ID: auto_save_channel_id must be a number.", ephemeral=True); return
        # Merge over the existing config so the Tier-3 keys set by
        # /transcriptconfig2 (disable_html_attachment, save_on_delete,
        # transcript_format) are not reset to their defaults.
        cfg = {
            **tr_mod.get_config(pdb, ctx.guild.id),
            'guild_id': ctx.guild.id, 'save_mode': save_mode,
            'auto_dm': int(auto_dm), 'custom_message': custom_message,
            'custom_title': custom_title,
            'auto_save_channel_id': archive_id,
            'enabled': int(enabled),
        }
        tr_mod.save_config(pdb, ctx.guild.id, cfg)
        embed = discord.Embed(title="📋 Transcript Config Saved", color=discord.Color.green())
        for k, v in cfg.items():
            embed.add_field(name=k, value=str(v)[:200], inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # ADVANCED SLA (Feature 9)
    # =================================================================
    @bot.command(name="slaconfig", description="Configure SLA targets")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        first_response_hours="Hours allowed before first response SLA breaches (0=disabled)",
        resolution_hours="Hours allowed before resolution SLA breaches (0=disabled)",
        urgent_first_response_hours="Override for urgent-priority tickets",
        urgent_resolution_hours="Override for urgent-priority tickets",
        escalation_role_id="Role to ping on SLA breach",
        warn_before_breach_pct="Warn when this % of SLA window elapses (0=disabled)",
        enabled="Enable SLA tracking (true/false)",
    )
    async def slaconfig_cmd(ctx: commands.Context, first_response_hours: float = 0.0,
                             resolution_hours: float = 0.0,
                             urgent_first_response_hours: float = 0.0,
                             urgent_resolution_hours: float = 0.0,
                             escalation_role_id: Optional[str] = None,
                             warn_before_breach_pct: int = 0,
                             enabled: bool = True) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        try:
            rid = _parse_id(escalation_role_id)
        except ValueError:
            await ctx.send("Invalid ID: escalation_role_id must be a number.", ephemeral=True); return
        cfg = {
            'guild_id': ctx.guild.id,
            'first_response_hours': first_response_hours,
            'resolution_hours': resolution_hours,
            'urgent_first_response_hours': urgent_first_response_hours,
            'urgent_resolution_hours': urgent_resolution_hours,
            'escalation_role_id': rid,
            'warn_before_breach_pct': warn_before_breach_pct,
            'enabled': int(enabled),
        }
        sla_mod.save_config(pdb, ctx.guild.id, cfg)
        embed = discord.Embed(title="⏱️ SLA Config Saved", color=discord.Color.green())
        for k, v in cfg.items():
            embed.add_field(name=k, value=str(v)[:200], inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="slareport", description="View SLA performance statistics")
    @commands.has_permissions(manage_channels=True)
    async def slareport_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rep = sla_mod.sla_report(pdb, guild_id=ctx.guild.id)
        embed = discord.Embed(title=f"⏱️ SLA Report — {ctx.guild.name}", color=discord.Color.blurple())
        embed.add_field(name="First response met", value=str(rep['first_response_met']), inline=True)
        embed.add_field(name="First response breached", value=str(rep['first_response_breached']), inline=True)
        embed.add_field(name="Resolution met", value=str(rep['resolution_met']), inline=True)
        embed.add_field(name="Resolution breached", value=str(rep['resolution_breached']), inline=True)
        embed.add_field(name="Avg first response", value=f"{rep['avg_first_response_minutes']} min" if rep['avg_first_response_minutes'] else "N/A", inline=True)
        embed.add_field(name="Avg resolution", value=f"{rep['avg_resolution_minutes']} min" if rep['avg_resolution_minutes'] else "N/A", inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # ANALYTICS + CSAT (Features 7 + 15)
    # =================================================================
    @bot.command(name="analytics", description="View ticket analytics overview")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(days="Number of days for the trend window (default 30)")
    async def analytics_cmd(ctx: commands.Context, days: int = 30) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rep = an_mod.build_overview(pdb, ctx.guild.id, days=days)
        embed = discord.Embed(title=f"📊 Ticket Analytics — {ctx.guild.name}", color=discord.Color.blurple())
        embed.add_field(name="Total tickets", value=str(rep['total']), inline=True)
        embed.add_field(name="Open", value=str(rep['open']), inline=True)
        embed.add_field(name="Closed", value=str(rep['closed']), inline=True)
        embed.add_field(name="Claimed (open)", value=str(rep['claimed_open']), inline=True)
        embed.add_field(name="Claimed (closed)", value=str(rep['claimed_closed']), inline=True)
        # Trend (last 7 days compact)
        if rep['trend']:
            last7 = rep['trend'][-7:]
            trend_lines = [f"`{t['date'][-5:]}` ➕{t['created']} ➖{t['closed']}" for t in last7]
            embed.add_field(name="Trend (last 7 days)", value="\n".join(trend_lines), inline=False)
        # By panel
        if rep['by_panel']:
            panel_lines = [f"• {p['panel_name']}: {p['total']} total ({p['open']} open)" for p in rep['by_panel'][:10]]
            embed.add_field(name="By panel", value="\n".join(panel_lines), inline=False)
        # By priority
        prio = rep['by_priority']
        if prio:
            prio_lines = [f"• {k}: {v}" for k, v in prio.items()]
            embed.add_field(name="Open by priority", value="\n".join(prio_lines), inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="csat", description="View CSAT (customer satisfaction) analytics")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(by="Group by: staff, panel, or time")
    async def csat_cmd(ctx: commands.Context, by: str = "staff") -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rep = an_mod.build_csat_report(pdb, ctx.guild.id, by=by)
        embed = discord.Embed(title=f"⭐ CSAT Report — {ctx.guild.name}", color=discord.Color.gold())
        embed.add_field(name="Avg rating", value=str(rep['avg']) or "N/A", inline=True)
        embed.add_field(name="Ratings count", value=str(rep['count']), inline=True)
        embed.add_field(name="Positive %", value=f"{rep['positive_pct']}%" if rep['positive_pct'] is not None else "N/A", inline=True)
        embed.add_field(name="Negative %", value=f"{rep['negative_pct']}%" if rep['negative_pct'] is not None else "N/A", inline=True)
        if rep['breakdown']:
            lines = []
            for b in rep['breakdown'][:10]:
                if by == 'staff':
                    lines.append(f"• <@{b['staff_id']}>: avg {b['avg']} ({b['count']} ratings, {b['positive_pct']}% positive)")
                elif by == 'panel':
                    lines.append(f"• {b['panel_name']}: avg {b['avg']} ({b['count']} ratings)")
                elif by == 'time':
                    lines.append(f"• {b['week']}: avg {b['avg']} ({b['count']} ratings)")
            embed.add_field(name=f"Breakdown by {by}", value="\n".join(lines), inline=False)
        if rep['feedback']:
            fb_lines = [f"• `{f['ticket_id']}` ({'⭐'*f['rating']}): {f['feedback'][:80]}" for f in rep['feedback'][:5]]
            embed.add_field(name="Recent feedback", value="\n".join(fb_lines), inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="staffstats", description="View per-staff ticket performance")
    @commands.has_permissions(manage_channels=True)
    async def staffstats_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rows = an_mod.build_staff_report(pdb, ctx.guild.id)
        if not rows:
            await ctx.send("No staff ticket data yet.", ephemeral=True); return
        embed = discord.Embed(title=f"👥 Staff Performance — {ctx.guild.name}", color=discord.Color.blurple())
        for r in rows[:12]:
            avg_r = f"⭐ {r['avg_rating']}" if r['avg_rating'] is not None else "N/A"
            avg_fr = f"{r['avg_first_response_minutes']}m" if r['avg_first_response_minutes'] is not None else "N/A"
            avg_res = f"{r['avg_resolution_minutes']}m" if r['avg_resolution_minutes'] is not None else "N/A"
            embed.add_field(
                name=f"<@{r['staff_id']}>",
                value=f"Claimed: {r['tickets_claimed']} • Closed: {r['tickets_closed']}\n"
                      f"CSAT: {avg_r} ({r['ratings_count']} ratings)\n"
                      f"Avg first response: {avg_fr} • Avg resolution: {avg_res}",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="export", description="Export all tickets as CSV")
    @commands.has_permissions(manage_channels=True)
    async def export_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        csv_str = an_mod.export_tickets_csv(pdb, ctx.guild.id)
        if not csv_str:
            await ctx.send("No tickets to export.", ephemeral=True); return
        from io import BytesIO
        await ctx.send(
            file=discord.File(BytesIO(csv_str.encode('utf-8')), filename=f"tickets-{ctx.guild.id}.csv"),
            ephemeral=True,
        )


    # =================================================================
    # KNOWLEDGE BASE (Tier 2 Features #12 + #13)
    # =================================================================
    @bot.command(name="kb", description="Knowledge base commands")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        action="add, view, list, search, remove, stats, or category",
        article_id="Article ID (for view/remove)",
        category="Category name (for add/list)",
        title="Article title (for add)",
        content="Article content (for add)",
        summary="Short summary (optional)",
        keywords="Comma-separated keywords (optional)",
        staff_only="Restrict to staff (true/false, default false)",
    )
    async def kb_cmd(ctx: commands.Context, action: str,
                      article_id: Optional[str] = None,
                      category: Optional[str] = None,
                      title: Optional[str] = None,
                      content: Optional[str] = None,
                      summary: Optional[str] = None,
                      keywords: Optional[str] = None,
                      staff_only: bool = False) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        action = action.lower().strip()
        if action == 'add':
            if not (title and content):
                await ctx.send("Usage: /kb add <title> <content> [category]", ephemeral=True); return
            aid = kb_mod.create_article(pdb, guild_id=ctx.guild.id, category=category,
                                          title=title, content=content, summary=summary,
                                          keywords=keywords, staff_only=staff_only,
                                          created_by=ctx.author.id)
            await ctx.send(f"📚 Article created: `{aid}`", ephemeral=True)
        elif action == 'view':
            if not article_id:
                await ctx.send("Usage: /kb view <article_id>", ephemeral=True); return
            is_staff = ctx.author.guild_permissions.manage_channels
            article = kb_mod.get_article(pdb, article_id, viewer_is_staff=is_staff)
            if not article:
                await ctx.send("Article not found.", ephemeral=True); return
            await ctx.send(embed=kb_mod.build_article_embed(article))
        elif action == 'list':
            is_staff = ctx.author.guild_permissions.manage_channels
            articles = kb_mod.list_articles(pdb, ctx.guild.id, category=category,
                                              viewer_is_staff=is_staff)
            await ctx.send(embed=kb_mod.build_list_embed(articles), ephemeral=True)
        elif action == 'search':
            if not article_id:  # reuse the article_id slot for the query
                await ctx.send("Usage: /kb search <query> (put the query in article_id)", ephemeral=True); return
            is_staff = ctx.author.guild_permissions.manage_channels
            results = kb_mod.search(pdb, ctx.guild.id, article_id, viewer_is_staff=is_staff)
            await ctx.send(embed=kb_mod.build_search_embed(results, article_id), ephemeral=True)
        elif action == 'remove':
            if not article_id:
                await ctx.send("Usage: /kb remove <article_id>", ephemeral=True); return
            if kb_mod.delete_article(pdb, article_id):
                await ctx.send(f"Article `{article_id}` removed.", ephemeral=True)
            else:
                await ctx.send("Article not found.", ephemeral=True)
        elif action == 'stats':
            await ctx.send(embed=kb_mod.build_stats_embed(kb_mod.stats(pdb, ctx.guild.id)), ephemeral=True)
        elif action == 'category':
            if not category:
                cats = kb_mod.list_categories(pdb, ctx.guild.id)
                if not cats:
                    await ctx.send("No categories.", ephemeral=True); return
                lines = [f"`{c['category_id']}` — {c['name']}" for c in cats]
                await ctx.send("\n".join(lines), ephemeral=True); return
            cid = kb_mod.create_category(pdb, guild_id=ctx.guild.id, name=category,
                                          description=summary, staff_only=staff_only)
            await ctx.send(f"Category created: `{cid}`", ephemeral=True)
        else:
            await ctx.send("Actions: add, view, list, search, remove, stats, category", ephemeral=True)

    # =================================================================
    # THREAD TICKETS (Tier 2 Feature #21)
    # =================================================================
    @bot.command(name="threadtickets", description="Configure thread-based tickets for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        enabled="Enable thread tickets (true/false)",
        parent_channel_id="Parent text channel to create threads under",
        allow_user_invite="Allow non-staff to invite others into the thread (true/false)",
    )
    async def threadtickets_cmd(ctx: commands.Context, panel_id: str, enabled: bool,
                                  parent_channel_id: Optional[str] = None,
                                  allow_user_invite: bool = False) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            await ctx.send("Ticket system not initialized.", ephemeral=True); return
        pid = int(parent_channel_id.lstrip('<#').rstrip('>')) if parent_channel_id else None
        ok = tt_mod.configure_panel_for_threads(pdb, tt.data_manager, panel_id, ctx.guild.id,
                                                  enabled=enabled, parent_channel_id=pid,
                                                  allow_user_invite=allow_user_invite)
        if not ok:
            await ctx.send(f"Panel `{panel_id}` not found.", ephemeral=True); return
        embed = discord.Embed(title="🧵 Thread Tickets Configured", color=discord.Color.green())
        embed.add_field(name="Panel", value=panel_id, inline=False)
        embed.add_field(name="Enabled", value="✅" if enabled else "❌", inline=True)
        embed.add_field(name="Parent channel", value=f"<#{pid}>" if pid else "(not set)", inline=True)
        embed.add_field(name="Allow user invite", value="✅" if allow_user_invite else "❌", inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # STAFF DISCUSSION THREADS (Tier 2 Feature #22)
    # =================================================================
    @bot.command(name="staffthread", description="Configure private staff discussion threads for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID", enabled="Enable staff threads (true/false)")
    async def staffthread_cmd(ctx: commands.Context, panel_id: str, enabled: bool) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            await ctx.send("Ticket system not initialized.", ephemeral=True); return
        ok = st_mod.configure_panel(pdb, tt.data_manager, panel_id, enabled=enabled)
        if not ok:
            await ctx.send(f"Panel `{panel_id}` not found.", ephemeral=True); return
        await ctx.send(f"🔒 Staff discussion threads {'enabled' if enabled else 'disabled'} for panel `{panel_id}`.", ephemeral=True)

    # =================================================================
    # CHANNEL RECYCLING (Tier 2 Feature #24)
    # =================================================================
    @bot.command(name="channelrecycle", description="Configure ticket channel recycling for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID", enabled="Enable recycling (true/false)")
    async def channelrecycle_cmd(ctx: commands.Context, panel_id: str, enabled: bool) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            await ctx.send("Ticket system not initialized.", ephemeral=True); return
        ok = cr_mod.configure_panel(pdb, tt.data_manager, panel_id, enabled=enabled)
        if not ok:
            await ctx.send(f"Panel `{panel_id}` not found.", ephemeral=True); return
        stats = cr_mod.pool_stats(pdb, panel_id)
        await ctx.send(
            f"♻️ Channel recycling {'enabled' if enabled else 'disabled'} for panel `{panel_id}`.\n"
            f"Recycled channels in pool: {stats['total']}",
            ephemeral=True,
        )

    # =================================================================
    # LOCALIZATION (Tier 2 Feature #28)
    # =================================================================
    @bot.command(name="locale", description="Set the guild's language")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        language="Language code: en, es, fr, de",
        timezone="Timezone (optional, e.g. ET, PT, GMT, CET)",
    )
    async def locale_cmd(ctx: commands.Context, language: str,
                           timezone: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if not i18n_mod.set_language(pdb, ctx.guild.id, language, timezone):
            await ctx.send(f"Unsupported language. Use one of: {', '.join(i18n_mod.SUPPORTED_LANGUAGES.keys())}", ephemeral=True); return
        await ctx.send(f"🌍 Language set to **{i18n_mod.SUPPORTED_LANGUAGES[language]}**.", ephemeral=True)

    @bot.command(name="localestring", description="Override a specific string")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(string_key="String key (use /localelist to see keys)", value="Custom text")
    async def localestring_cmd(ctx: commands.Context, string_key: str, value: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        i18n_mod.set_custom_string(pdb, ctx.guild.id, string_key, value)
        await ctx.send(f"✅ String `{string_key}` overridden.", ephemeral=True)

    @bot.command(name="localelist", description="List all localizable strings")
    @commands.has_permissions(manage_channels=True)
    async def localelist_cmd(ctx: commands.Context) -> None:
        strings = i18n_mod.list_available_strings()
        embed = discord.Embed(title="🌍 Localizable Strings", color=discord.Color.blurple())
        lines = [f"`{k}` — {v.get('en','')}" for k, v in list(strings.items())[:25]]
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Use /localestring <key> <value> to override. {len(strings)} strings total.")
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # BRANDED REPLIES (Tier 2 Feature #29)
    # =================================================================
    @bot.command(name="brandedreplies", description="Configure anonymous/branded staff replies")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        action="setup, disable, or view",
        display_name="The shared name shown on replies (e.g. GANG Support)",
        avatar_url="Avatar URL for the branded identity (optional)",
    )
    async def brandedreplies_cmd(ctx: commands.Context, panel_id: str, action: str,
                                   display_name: Optional[str] = None,
                                   avatar_url: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            await ctx.send("Ticket system not initialized.", ephemeral=True); return
        panel = tt.data_manager.load_ticket_panel(panel_id)
        if not panel:
            await ctx.send(f"Panel `{panel_id}` not found.", ephemeral=True); return
        action = action.lower().strip()
        if action == 'setup':
            if not display_name:
                await ctx.send("display_name is required for setup.", ephemeral=True); return
            cfg = await br_mod.setup_webhook(
                guild=ctx.guild, panel=panel, display_name=display_name,
                avatar_url=avatar_url, pdb=pdb,
            )
            if not cfg:
                await ctx.send("Failed to set up webhook. Check bot permissions.", ephemeral=True); return
            await ctx.send(f"🎨 Branded replies enabled for panel `{panel_id}` as **{display_name}**.", ephemeral=True)
        elif action == 'disable':
            br_mod.disable(pdb, panel_id)
            await ctx.send(f"🎨 Branded replies disabled for panel `{panel_id}`.", ephemeral=True)
        elif action == 'view':
            cfg = br_mod.get_config(pdb, panel_id)
            embed = discord.Embed(title="🎨 Branded Replies Config", color=discord.Color.blurple())
            embed.add_field(name="Enabled", value="✅" if cfg.get('enabled') else "❌", inline=True)
            embed.add_field(name="Display name", value=cfg.get('display_name') or '(default)', inline=True)
            if cfg.get('avatar_url'):
                embed.add_field(name="Avatar", value="[link]({cfg['avatar_url']})", inline=False)
            await ctx.send(embed=embed, ephemeral=True)
        else:
            await ctx.send("Actions: setup, disable, view", ephemeral=True)

    # =================================================================
    # SUPPORT FLOWS (Tier 2 Feature #30)
    # =================================================================
    @bot.command(name="flow", description="Create or update a support flow")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        name="Flow name",
        steps_json="JSON list of steps (see /flowhelp for format)",
        description="Flow description (optional)",
        flow_id="Existing flow ID to update (optional)",
    )
    async def flow_cmd(ctx: commands.Context, name: str, steps_json: str,
                         description: Optional[str] = None,
                         flow_id: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as e:
            await ctx.send(f"Invalid steps JSON: {e}", ephemeral=True); return
        try:
            fid = flow_mod.create_or_update_flow(pdb, guild_id=ctx.guild.id, name=name,
                                                  description=description, steps=steps,
                                                  flow_id=flow_id)
        except ValueError as e:
            await ctx.send(f"Invalid flow: {e}", ephemeral=True); return
        await ctx.send(f"📝 Flow `{fid}` saved with {len(steps)} step(s).", ephemeral=True)

    @bot.command(name="flowlist", description="List all support flows")
    @commands.has_permissions(manage_channels=True)
    async def flowlist_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        flows = flow_mod.list_flows(pdb, ctx.guild.id)
        if not flows:
            await ctx.send("No flows configured.", ephemeral=True); return
        embed = discord.Embed(title="📝 Support Flows", color=discord.Color.blurple())
        for f in flows[:15]:
            embed.add_field(
                name=f"`{f['flow_id']}` — {f.get('name','?')}",
                value=f"{len(f.get('steps',[]))} step(s) • Start: {f.get('start_step_id','?')}",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="flowattach", description="Attach a flow to a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID", flow_id="Flow ID (or empty to detach)")
    async def flowattach_cmd(ctx: commands.Context, panel_id: str,
                               flow_id: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            await ctx.send("Ticket system not initialized.", ephemeral=True); return
        ok = flow_mod.attach_flow_to_panel(pdb, tt.data_manager, panel_id, flow_id)
        if not ok:
            await ctx.send(f"Panel `{panel_id}` not found.", ephemeral=True); return
        await ctx.send(f"📝 Flow {'attached' if flow_id else 'detached'} for panel `{panel_id}`.", ephemeral=True)

    @bot.command(name="flowdelete", description="Delete a support flow")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(flow_id="Flow ID")
    async def flowdelete_cmd(ctx: commands.Context, flow_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if flow_mod.delete_flow(pdb, flow_id):
            await ctx.send(f"Flow `{flow_id}` deleted.", ephemeral=True)
        else:
            await ctx.send("Flow not found.", ephemeral=True)

    # =================================================================
    # CUSTOM COMMANDS (Tier 2 Feature #36)
    # =================================================================
    @bot.command(name="customcommand", description="Create or update a custom command")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        name="Command name (lowercase, no spaces)",
        actions_json="JSON list of actions (same format as automations)",
        description="What this command does (optional)",
        required_role_id="Role required to use this command (optional)",
        ticket_only="Restrict to ticket channels (true/false)",
        cooldown_seconds="Cooldown between uses per user (default 0)",
    )
    async def customcommand_cmd(ctx: commands.Context, name: str, actions_json: str,
                                  description: Optional[str] = None,
                                  required_role_id: Optional[str] = None,
                                  ticket_only: bool = False,
                                  cooldown_seconds: int = 0) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        try:
            actions = json.loads(actions_json)
        except json.JSONDecodeError as e:
            await ctx.send(f"Invalid actions JSON: {e}", ephemeral=True); return
        try:
            rid = _parse_id(required_role_id)
        except ValueError:
            await ctx.send("Invalid ID: required_role_id must be a number.", ephemeral=True); return
        try:
            cid = cc_mod.create_or_update(pdb, guild_id=ctx.guild.id, name=name,
                                            actions=actions, description=description,
                                            required_role_id=rid, ticket_only=ticket_only,
                                            cooldown_seconds=cooldown_seconds,
                                            created_by=ctx.author.id)
        except ValueError as e:
            await ctx.send(f"Invalid command: {e}", ephemeral=True); return
        await ctx.send(f"⚙️ Custom command `!{name.lower()}` saved ({cid}).", ephemeral=True)

    @bot.command(name="customcommandlist", description="List all custom commands")
    @commands.has_permissions(manage_channels=True)
    async def customcommandlist_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        cmds = cc_mod.list_commands(pdb, ctx.guild.id)
        if not cmds:
            await ctx.send("No custom commands.", ephemeral=True); return
        embed = discord.Embed(title="⚙️ Custom Commands", color=discord.Color.blurple())
        for c in cmds[:15]:
            embed.add_field(
                name=f"!{c['name']}",
                value=f"`{c['command_id']}` • {len(c.get('actions',[]))} action(s) • cooldown {c.get('cooldown_seconds',0)}s",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="customcommandremove", description="Remove a custom command")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(name="Command name")
    async def customcommandremove_cmd(ctx: commands.Context, name: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        cmd = cc_mod.get_command(pdb, ctx.guild.id, name)
        if not cmd:
            await ctx.send(f"Command `!{name}` not found.", ephemeral=True); return
        cc_mod.delete(pdb, cmd['command_id'])
        await ctx.send(f"Removed custom command `!{name}`.", ephemeral=True)

    # =================================================================
    # ADVANCED STAFF ANALYTICS (Tier 2 Feature #25)
    # =================================================================
    @bot.command(name="staffanalytics", description="Advanced per-staff analytics")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(days="Look back this many days (default 30)")
    async def staffanalytics_cmd(ctx: commands.Context, days: int = 30) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rows = an_mod.build_advanced_staff_report(pdb, ctx.guild.id, days=days)
        if not rows:
            await ctx.send("No staff ticket data in this period.", ephemeral=True); return
        embed = discord.Embed(
            title=f"👥 Advanced Staff Analytics — last {days}d",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        for r in rows[:8]:
            fr_avg = f"{r['avg_first_response_minutes']}m" if r['avg_first_response_minutes'] else "N/A"
            fr_p90 = f"{r['p90_first_response_minutes']}m" if r['p90_first_response_minutes'] else "N/A"
            res_avg = f"{r['avg_resolution_minutes']}m" if r['avg_resolution_minutes'] else "N/A"
            peak = f"{r['peak_hour']:02d}:00" if r['peak_hour'] is not None else "N/A"
            embed.add_field(
                name=f"<@{r['staff_id']}>",
                value=(f"Claimed: {r['tickets_claimed']} • Closed: {r['tickets_closed']}\n"
                       f"FR avg: {fr_avg} (p90 {fr_p90}) • Res avg: {res_avg}\n"
                       f"Msgs/ticket: {r['avg_messages_per_ticket']} • Escalations: {r['escalations']}\n"
                       f"Active days: {r['active_days']} • Peak hour: {peak}"),
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="ticktrends", description="Long-term ticket trend report")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(days="Look back this many days (default 90)")
    async def ticktrends_cmd(ctx: commands.Context, days: int = 90) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rep = an_mod.build_ticket_trend_report(pdb, ctx.guild.id, days=days)
        embed = discord.Embed(title=f"📈 Ticket Trends — last {days}d", color=discord.Color.blurple())
        embed.add_field(name="Total created", value=str(rep['total_created']), inline=True)
        embed.add_field(name="Total closed", value=str(rep['total_closed']), inline=True)
        embed.add_field(name="Avg/day created", value=str(rep['avg_per_day_created']), inline=True)
        embed.add_field(name="Avg/day closed", value=str(rep['avg_per_day_closed']), inline=True)
        if rep['busiest_day']:
            b = rep['busiest_day']
            embed.add_field(name="Busiest day", value=f"{b['date']} ({b['created']} tickets)", inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="responsedistribution", description="First-response time distribution")
    @commands.has_permissions(manage_channels=True)
    async def responsedistribution_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        buckets = an_mod.build_response_distribution(pdb, ctx.guild.id)
        embed = discord.Embed(title="⏱️ First-Response Distribution", color=discord.Color.gold())
        total = sum(buckets.values()) or 1
        for bucket, count in buckets.items():
            pct = round(100 * count / total, 1)
            bar = '█' * int(pct / 5) + '░' * (20 - int(pct / 5))
            embed.add_field(name=bucket, value=f"`{bar}` {count} ({pct}%)", inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # MULTI-EMBED PANEL MESSAGES (Tier 3 Feature #10)
    # =================================================================
    @bot.command(name="panelembed", description="Add or update a multi-embed for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        embed_json='JSON embed data: {"title":"...","description":"...","color":"0x5865F2","fields":[{"name":"...","value":"...","inline":true}]}',
        embed_id="Existing embed ID to update (optional, for updates)",
        order_index="Position in the embed list (0=first, optional)",
    )
    async def panelembed_cmd(ctx: commands.Context, panel_id: str, embed_json: str,
                               embed_id: Optional[str] = None,
                               order_index: Optional[int] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        try:
            embed_data = json.loads(embed_json)
        except json.JSONDecodeError as e:
            await ctx.send(f"Invalid embed JSON: {e}", ephemeral=True); return
        try:
            if embed_id:
                me_mod.update_panel_embed(pdb, embed_id, embed_data)
                await ctx.send(f"📝 Embed `{embed_id}` updated.", ephemeral=True)
            else:
                eid = me_mod.add_panel_embed(pdb, panel_id=panel_id, guild_id=ctx.guild.id,
                                               embed_data=embed_data, order_index=order_index)
                await ctx.send(f"📝 Embed added: `{eid}`", ephemeral=True)
        except ValueError as e:
            await ctx.send(str(e), ephemeral=True)

    @bot.command(name="panelembedlist", description="List all multi-embeds for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID")
    async def panelembedlist_cmd(ctx: commands.Context, panel_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        embeds = pdb.list_panel_embeds(panel_id)
        if not embeds:
            await ctx.send("No multi-embeds configured for that panel.", ephemeral=True); return
        embed = discord.Embed(title=f"📝 Panel Embeds — {panel_id}", color=discord.Color.blurple())
        for e in embeds[:10]:
            data = e.get('embed_data', {})
            title = data.get('title', '(no title)')
            nfields = len(data.get('fields', []))
            embed.add_field(
                name=f"`{e['embed_id']}` (order {e.get('order_index',0)})",
                value=f"Title: {title}\nFields: {nfields}",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="panelembedremove", description="Remove a multi-embed from a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(embed_id="Embed ID to remove")
    async def panelembedremove_cmd(ctx: commands.Context, embed_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if me_mod.remove_panel_embed(pdb, embed_id):
            await ctx.send(f"Embed `{embed_id}` removed.", ephemeral=True)
        else:
            await ctx.send("Embed not found.", ephemeral=True)

    @bot.command(name="panelembedenable", description="Enable/disable multi-embed mode for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID", enabled="Enable multi-embed (true/false)")
    async def panelembedenable_cmd(ctx: commands.Context, panel_id: str, enabled: bool) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            await ctx.send("Ticket system not initialized.", ephemeral=True); return
        ok = me_mod.enable_multi_embed(pdb, tt.data_manager, panel_id, enabled=enabled)
        if not ok:
            await ctx.send(f"Panel `{panel_id}` not found.", ephemeral=True); return
        await ctx.send(f"📝 Multi-embed {'enabled' if enabled else 'disabled'} for panel `{panel_id}`.", ephemeral=True)

    # =================================================================
    # ADVANCED MODERATOR MESSAGES (Tier 3 Feature #9)
    # =================================================================
    @bot.command(name="modmessage", description="Configure a moderator message for a ticket event")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID",
        event_type="Event: close, reopen, delete, claim, unclaim, create",
        content="Plain text content (supports variables, optional)",
        embeds_json='JSON list of embeds (optional): [{"title":"...","description":"..."}]',
        buttons_json='JSON list of buttons (optional): [{"label":"...","style":"success","custom_id":"..."}]',
        enabled="Enable this message (true/false)",
    )
    async def modmessage_cmd(ctx: commands.Context, panel_id: str, event_type: str,
                              content: Optional[str] = None,
                              embeds_json: Optional[str] = None,
                              buttons_json: Optional[str] = None,
                              enabled: bool = True) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if event_type not in mm_mod.MODERATOR_EVENT_TYPES:
            await ctx.send(f"Invalid event_type. Use: {', '.join(sorted(mm_mod.MODERATOR_EVENT_TYPES))}", ephemeral=True); return
        embeds = []
        if embeds_json:
            try:
                embeds = json.loads(embeds_json)
            except json.JSONDecodeError as e:
                await ctx.send(f"Invalid embeds JSON: {e}", ephemeral=True); return
        buttons = []
        if buttons_json:
            try:
                buttons = json.loads(buttons_json)
            except json.JSONDecodeError as e:
                await ctx.send(f"Invalid buttons JSON: {e}", ephemeral=True); return
        mid = mm_mod.set_moderator_message(pdb, panel_id=panel_id, guild_id=ctx.guild.id,
                                              event_type=event_type, content=content,
                                              embeds=embeds, buttons=buttons, enabled=enabled)
        embed = discord.Embed(title="📝 Moderator Message Saved", color=discord.Color.green())
        embed.add_field(name="ID", value=f"`{mid}`", inline=True)
        embed.add_field(name="Panel", value=panel_id, inline=True)
        embed.add_field(name="Event", value=event_type, inline=True)
        embed.add_field(name="Content", value=(content or '(none)')[:200], inline=False)
        embed.add_field(name="Embeds", value=str(len(embeds)), inline=True)
        embed.add_field(name="Buttons", value=str(len(buttons)), inline=True)
        embed.add_field(name="Enabled", value="✅" if enabled else "❌", inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="modmessagelist", description="List all moderator messages for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID")
    async def modmessagelist_cmd(ctx: commands.Context, panel_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        rows = pdb.list_moderator_messages(panel_id)
        if not rows:
            await ctx.send("No moderator messages configured for that panel.", ephemeral=True); return
        embed = discord.Embed(title=f"📝 Moderator Messages — {panel_id}", color=discord.Color.blurple())
        for r in rows[:15]:
            embed.add_field(
                name=f"`{r['message_id']}` — {r['event_type']}",
                value=f"Enabled: {'✅' if r.get('enabled') else '❌'} • Embeds: {len(r.get('embeds',[]))} • Buttons: {len(r.get('buttons',[]))}",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True)

    @bot.command(name="modmessageremove", description="Remove a moderator message")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(message_id="Moderator message ID to remove")
    async def modmessageremove_cmd(ctx: commands.Context, message_id: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        if mm_mod.delete_moderator_message(pdb, message_id):
            await ctx.send(f"Moderator message `{message_id}` removed.", ephemeral=True)
        else:
            await ctx.send("Message not found.", ephemeral=True)

    # =================================================================
    # FLOW REVIEW / APPROVAL (Tier 3 Feature #32)
    # =================================================================
    @bot.command(name="flowapplication", description="Mark a flow as an application (routes to review queue)")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(flow_id="Flow ID", is_application="True for application flow, false for normal")
    async def flowapplication_cmd(ctx: commands.Context, flow_id: str, is_application: bool) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        tt = getattr(bot, 'ticket_tool', None)
        if tt is None:
            await ctx.send("Ticket system not initialized.", ephemeral=True); return
        ok = fr_mod.set_flow_application_flag(pdb, tt.data_manager, flow_id, is_application=is_application)
        if not ok:
            await ctx.send(f"Flow `{flow_id}` not found.", ephemeral=True); return
        await ctx.send(f"📝 Flow `{flow_id}` marked as {'application' if is_application else 'normal'}.", ephemeral=True)

    @bot.command(name="flowreviewconfig", description="Configure review settings for an application flow")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        flow_id="Flow ID",
        review_channel_id="Channel to post review messages in",
        reviewer_role_id="Role to ping for reviews (optional)",
        auto_approve_minutes="Auto-approve after N minutes (0=disabled)",
        auto_reject_minutes="Auto-reject after N minutes (0=disabled)",
        approved_panel_id="Panel to route approved applicants to (optional)",
        rejected_panel_id="Panel to route rejected applicants to (optional)",
    )
    async def flowreviewconfig_cmd(ctx: commands.Context, flow_id: str,
                                     review_channel_id: str,
                                     reviewer_role_id: Optional[str] = None,
                                     auto_approve_minutes: int = 0,
                                     auto_reject_minutes: int = 0,
                                     approved_panel_id: Optional[str] = None,
                                     rejected_panel_id: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        try:
            rch = _parse_id(review_channel_id, mention_prefix='<#')
        except ValueError:
            await ctx.send("Invalid ID: review_channel_id must be a number.", ephemeral=True); return
        if rch is None:
            await ctx.send("review_channel_id is required.", ephemeral=True); return
        try:
            rrid = _parse_id(reviewer_role_id)
        except ValueError:
            await ctx.send("Invalid ID: reviewer_role_id must be a number.", ephemeral=True); return
        fr_mod.save_config(pdb, flow_id=flow_id, guild_id=ctx.guild.id,
                             review_channel_id=rch, reviewer_role_id=rrid,
                             auto_approve_minutes=auto_approve_minutes,
                             auto_reject_minutes=auto_reject_minutes,
                             approved_panel_id=approved_panel_id,
                             rejected_panel_id=rejected_panel_id)
        await ctx.send(f"📝 Review config saved for flow `{flow_id}`.", ephemeral=True)

    @bot.command(name="reviewpending", description="List pending application reviews")
    @commands.has_permissions(manage_channels=True)
    async def reviewpending_cmd(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        reviews = fr_mod.list_pending(pdb, ctx.guild.id)
        await ctx.send(embed=fr_mod.build_pending_embed(reviews), ephemeral=True)

    @bot.command(name="reviewdecision", description="Approve or reject a pending review")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(review_id="Review ID", decision="approve or reject", notes="Optional notes")
    async def reviewdecision_cmd(ctx: commands.Context, review_id: str, decision: str,
                                   notes: Optional[str] = None) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        decision = decision.lower().strip()
        if decision not in ('approve', 'reject'):
            await ctx.send("Decision must be 'approve' or 'reject'.", ephemeral=True); return
        approved = decision.startswith('approve')
        submission = pdb.get_review_submission(review_id)
        if not submission:
            await ctx.send("Review submission not found.", ephemeral=True); return
        if submission.get('status') != 'pending':
            await ctx.send(f"This submission has already been {submission.get('status')}.", ephemeral=True); return
        # Get the flow review config to find the target panel.
        cfg = pdb.get_flow_review_config(submission.get('flow_id'))
        if cfg is None:
            await ctx.send("Review config not found for this flow.", ephemeral=True); return
        panel_id = cfg.get('approved_panel_id') if approved else cfg.get('rejected_panel_id')
        # Shared decision core (also used by the review buttons + auto timers):
        # updates the status, notifies the applicant, routes to the panel.
        ok, msg = await fr_mod.apply_review_decision(
            bot=bot, pdb=pdb, review_id=review_id, approved=approved,
            panel_id=panel_id, actor=ctx.author, notes=notes,
        )
        if not ok:
            await ctx.send(msg, ephemeral=True); return
        # Update the original review message (recolor + decision + no buttons).
        try:
            await fr_mod.update_review_message(bot=bot, pdb=pdb, review_id=review_id,
                                               approved=approved, actor=ctx.author)
        except Exception as exc:
            logging.debug(f"[tickettool.commands] review message update failed: {exc}")
        await ctx.send(msg, ephemeral=True)

    # =================================================================
    # EXTENDED TRANSCRIPT CONFIG (Tier 3 Feature #11)
    # =================================================================
    @bot.command(name="transcriptconfig2", description="Extended transcript config (Tier 3)")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        disable_html_attachment="Don't attach HTML file (store in DB only) (true/false)",
        save_on_delete="Save transcript when ticket is deleted (true/false)",
        transcript_format="Format: html or text (default html)",
    )
    async def transcriptconfig2_cmd(ctx: commands.Context, disable_html_attachment: bool = False,
                                      save_on_delete: bool = True,
                                      transcript_format: str = 'html') -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True); return
        existing = tr_mod.get_config(pdb, ctx.guild.id)
        cfg = {
            **existing,
            'guild_id': ctx.guild.id,
            'disable_html_attachment': int(disable_html_attachment),
            'save_on_delete': int(save_on_delete),
            'transcript_format': transcript_format,
        }
        tr_mod.save_config(pdb, ctx.guild.id, cfg)
        embed = discord.Embed(title="📋 Extended Transcript Config", color=discord.Color.green())
        embed.add_field(name="Disable HTML attachment", value="✅" if disable_html_attachment else "❌", inline=True)
        embed.add_field(name="Save on delete", value="✅" if save_on_delete else "❌", inline=True)
        embed.add_field(name="Format", value=transcript_format, inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    # =================================================================
    # CANNED REPLIES (Ticket Tool !canned) — prefix-only command group
    # =================================================================
    @bot.group(name="canned", description="Canned replies: saved response snippets for tickets")
    async def canned_group(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True)
            return
        if ctx.invoked_subcommand is None:
            # /canned with no subcommand: prefix-mode shorthand — a name was
            # passed as the first token (e.g. "!canned greeting") → send it.
            arg = (ctx.message.content.split(maxsplit=1)[1] if ctx.message and len(ctx.message.content.split(maxsplit=1)) > 1 else None) if getattr(ctx, 'message', None) else None
            if arg and not arg.startswith(('-', ' ')) and ' ' not in arg.strip():
                result = await canned_mod.send_reply(
                    bot=bot, pdb=pdb, ctx_or_interaction=ctx,
                    channel=ctx.channel, guild=ctx.guild, name=arg.strip(),
                    staff_member=ctx.author,
                )
                if result['ok']:
                    return
                await ctx.send(result['error'], ephemeral=True)
                return
            await ctx.send(embed=canned_mod.build_list_embed(canned_mod.list_replies(pdb, ctx.guild.id))
                           if ctx.guild else "Use this in a server.", ephemeral=True)

    @canned_group.command(name="add", description="Save a canned reply")
    @app_commands.describe(name="Short name (letters/digits/-/_)", content="Reply text — supports {user.name}, {ticket.*} variables")
    @commands.has_permissions(manage_channels=True)
    async def canned_add(ctx: commands.Context, name: str, *, content: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True)
            return
        result = canned_mod.save_reply(pdb, ctx.guild.id, name, content, ctx.author.id)
        if not result['ok']:
            await ctx.send(result['error'], ephemeral=True)
            return
        verb = 'added' if result['created'] else 'updated'
        await ctx.send(embed=discord.Embed(
            description=f"✅ Canned reply `{canned_mod.validate_name(name)}` {verb}.",
            color=discord.Color.green(),
        ), ephemeral=True)

    @canned_group.command(name="send", description="Send a canned reply into this ticket")
    @app_commands.describe(name="Name of the canned reply")
    @commands.has_permissions(manage_channels=True)
    async def canned_send(ctx: commands.Context, name: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True)
            return
        result = await canned_mod.send_reply(
            bot=bot, pdb=pdb, ctx_or_interaction=ctx,
            channel=ctx.channel, guild=ctx.guild, name=name,
            staff_member=ctx.author,
        )
        if not result['ok']:
            await ctx.send(result['error'], ephemeral=True)

    @canned_group.command(name="edit", description="Update a canned reply's content")
    @app_commands.describe(name="Existing reply name", content="New content")
    @commands.has_permissions(manage_channels=True)
    async def canned_edit(ctx: commands.Context, name: str, *, content: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True)
            return
        existing = canned_mod.get_reply(pdb, ctx.guild.id, name)
        if not existing:
            await ctx.send(f"No canned reply named `{name}`. See `!canned list`.", ephemeral=True)
            return
        result = canned_mod.save_reply(pdb, ctx.guild.id, name, content, ctx.author.id)
        if not result['ok']:
            await ctx.send(result['error'], ephemeral=True)
            return
        await ctx.send(f"✅ Canned reply `{existing['name']}` updated.", ephemeral=True)

    @canned_group.command(name="delete", description="Delete a canned reply")
    @app_commands.describe(name="Existing reply name")
    @commands.has_permissions(manage_channels=True)
    async def canned_delete(ctx: commands.Context, name: str) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True)
            return
        if canned_mod.delete_reply(pdb, ctx.guild.id, name):
            await ctx.send(f"🗑️ Canned reply `{name}` deleted.", ephemeral=True)
        else:
            await ctx.send(f"No canned reply named `{name}`.", ephemeral=True)

    @canned_group.command(name="list", description="List all canned replies")
    @commands.has_permissions(manage_channels=True)
    async def canned_list(ctx: commands.Context) -> None:
        pdb = _pdb()
        if pdb is None:
            await ctx.send("Premium not initialized.", ephemeral=True)
            return
        await ctx.send(embed=canned_mod.build_list_embed(canned_mod.list_replies(pdb, ctx.guild.id)), ephemeral=True)

    logging.info("[tickettool.commands] registered Tier 1 + Tier 2 + Tier 3 prefix commands")
