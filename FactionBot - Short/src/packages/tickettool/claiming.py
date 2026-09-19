# -*- coding: utf-8 -*-
'''
TicketTool.claiming — Advanced Claiming System (Tier 1 Feature #2).

Extends Bot.py's basic atomic claim with Ticket Tool Premium features:
  * only_claimer_unclaim            — only the claimer can release their claim
  * claimer_and_owner_only_actions — only claimer + ticket owner may use
                                     add/remove/rename/move/priority/note
  * auto_replace_claimer            — a new claim silently replaces the old one
  * allow_owner_claim               — ticket creator may claim their own ticket
  * rename_on_claim                 — full claimed channel-name template
                                     (supports variables like {ticket.count},
                                     {claim.user}); the naming module's
                                     claimed_template takes precedence
  * move_category_on_claim          — move the ticket to a configured category
  * hide_from_other_staff           — remove other staff's view perm on claim,
                                       restore on unclaim
  * change_support_perms_on_claim   — demote support role to read-only until
                                       unclaim
  * claimed_message / unclaimed_message — custom embed text per panel

Integration (called from TicketTool.wiring):
  * on_ticket_claim(channel, ticket, claimer) runs AFTER the atomic claim
    succeeds and applies rename/move/role/perms/custom-message.
  * on_ticket_unclaim(channel, ticket, unclaimer) reverses the side effects.
  * gating.is_authorized(actor, action, ticket) is consulted by the
    add/remove/rename/move/priority/note commands to enforce
    claimer_and_owner_only_actions.

Atomicity: the claim itself stays in Bot.py's DataManager.atomic_claim_ticket
(which already handles the race). This module owns only the *side effects*
that happen after a successful claim, and the gating policy.
'''

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord

from .db import PremiumDB, _json_loads_list
from . import naming as naming_mod


# =====================================================================
# CONFIG ACCESSORS
# =====================================================================

def get_config(pdb: PremiumDB, panel_id: str) -> Dict:
    '''Return the advanced-claiming config for a panel, with sane defaults.'''
    cfg = pdb.get_claiming_config(panel_id)
    if not cfg:
        return {
            'panel_id': panel_id,
            'only_claimer_unclaim': 1,
            'claimer_and_owner_only_actions': 0,
            'auto_replace_claimer': 0,
            'allow_owner_claim': 0,
            'rename_on_claim': None,
            'move_category_on_claim': None,
            'hide_from_other_staff': 0,
            'change_support_perms_on_claim': 0,
            'claimed_message': None,
            'unclaimed_message': None,
        }
    return cfg


def save_config(pdb: PremiumDB, panel_id: str, guild_id: int, cfg: Dict) -> None:
    pdb.upsert_claiming_config({**cfg, 'panel_id': panel_id, 'guild_id': guild_id})


# =====================================================================
# GATING POLICY
# =====================================================================

# Actions subject to claimer_and_owner_only_actions gating.
GATED_ACTIONS = {'add', 'remove', 'rename', 'move', 'priority', 'note', 'escalate'}


def is_authorized(
    pdb: PremiumDB,
    *,
    actor: discord.Member,
    action: str,
    ticket: Dict,
    panel: Optional[Dict] = None,
    is_staff: bool = False,
    is_admin: bool = False,
) -> Tuple[bool, str]:
    '''Decide whether `actor` may perform `action` on `ticket`.

    Returns (authorized, reason). Admins always pass.
    '''
    if is_admin:
        return True, 'administrator'
    # Unclaim gating: only the claimer (or admin) may unclaim.
    if action == 'unclaim':
        cfg = get_config(pdb, panel.get('panel_id')) if panel else {}
        if cfg.get('only_claimer_unclaim'):
            claimed_by = ticket.get('claimed_by')
            if not claimed_by:
                return False, 'This ticket is not claimed.'
            if int(claimed_by) != actor.id:
                return False, 'Only the claimer can unclaim this ticket (or an admin).'
        return True, 'ok'

    # claimer_and_owner_only_actions gating for management actions.
    if action in GATED_ACTIONS:
        cfg = get_config(pdb, panel.get('panel_id')) if panel else {}
        if cfg.get('claimer_and_owner_only_actions'):
            claimed_by = ticket.get('claimed_by')
            creator_id = ticket.get('creator_id')
            # If ticket is claimed, only claimer + owner may act.
            if claimed_by and int(claimed_by) != actor.id and int(creator_id) != actor.id:
                return False, f'Only the claimer (<@{claimed_by}>) or the ticket owner may do this while the ticket is claimed.'
    return True, 'ok'


# =====================================================================
# PRE-CLAIM POLICY (decides whether a claim attempt should be allowed)
# =====================================================================

def should_allow_claim(
    pdb: PremiumDB,
    *,
    member: discord.Member,
    ticket: Dict,
    panel: Optional[Dict],
    is_staff: bool,
) -> Tuple[bool, str]:
    '''Decide whether `member` is allowed to claim `ticket`.

    Returns (allowed, reason). Honors allow_owner_claim.
    '''
    cfg = get_config(pdb, panel.get('panel_id')) if panel else {}
    creator_id = ticket.get('creator_id')

    # Ticket creator claiming their own ticket is normally blocked.
    if creator_id and int(creator_id) == member.id:
        if cfg.get('allow_owner_claim'):
            return True, 'owner-claim-allowed'
        return False, "You can't claim your own ticket. Please wait for staff to respond."

    if not is_staff:
        return False, 'Only staff can claim tickets.'
    return True, 'ok'


def should_auto_replace(pdb: PremiumDB, panel: Optional[Dict]) -> bool:
    cfg = get_config(pdb, panel.get('panel_id')) if panel else {}
    return bool(cfg.get('auto_replace_claimer'))


# =====================================================================
# POST-CLAIM SIDE EFFECTS
# =====================================================================

async def apply_claim_side_effects(
    *,
    bot,
    pdb: PremiumDB,
    channel: discord.TextChannel,
    ticket: Dict,
    panel: Optional[Dict],
    claimer: discord.Member,
    ticket_count: Optional[int] = None,
) -> None:
    '''Run AFTER a successful atomic claim.

    Applies: rename, move category, hide-from-other-staff, support-perm
    demotion, role add/remove (delegates to role_automation), and the custom
    claimed_message.

    All side effects are best-effort: a failure in one (e.g. missing
    category) is logged but does NOT roll back the claim itself.
    '''
    cfg = get_config(pdb, panel.get('panel_id')) if panel else {}
    guild = channel.guild

    # 1. Rename on claim.
    # rename_on_claim is a FULL channel-name template (supports variables
    # like {ticket.count}, {claim.user}, {user.name}); when the naming
    # module's claimed_template is configured it takes precedence.
    rename_to = None
    if cfg.get('rename_on_claim'):
        from . import variables
        naming_cfg = pdb.get_naming(panel.get('panel_id')) if panel else None
        ctx = variables.VariableContext(
            ticket=ticket,
            ticket_count=naming_mod._padded_count(naming_cfg, ticket_count),
            panel=panel or {},
            guild={'id': guild.id, 'name': guild.name},
            claim_user={'id': claimer.id, 'name': claimer.display_name},
            acting_user={'id': claimer.id, 'name': claimer.display_name},
        )
        rename_to = variables.render(str(cfg['rename_on_claim']), ctx)
    if panel:
        templated = naming_mod.compute_claimed_name(
            pdb, panel,
            guild={'id': guild.id, 'name': guild.name},
            ticket=ticket,
            claimer={'id': claimer.id, 'name': claimer.display_name},
            ticket_count=ticket_count,
        )
        if templated:
            rename_to = templated
    if rename_to:
        try:
            await channel.edit(name=_safe_channel_name(rename_to))
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.claiming] rename on claim failed: {exc}")

    # 2. Move category on claim.
    move_cat = cfg.get('move_category_on_claim')
    if move_cat:
        try:
            cat = guild.get_channel(int(move_cat))
            if isinstance(cat, discord.CategoryChannel):
                await channel.edit(category=cat)
        except (discord.HTTPException, ValueError, TypeError) as exc:
            logging.warning(f"[tickettool.claiming] move category on claim failed: {exc}")

    # 3. Hide from other staff (remove view perm from other support-role
    #    members). We cannot enumerate "all other staff" cheaply, so instead
    #    we set the channel to private and re-add only the claimer + admins.
    if cfg.get('hide_from_other_staff'):
        try:
            support_role_id = (panel or {}).get('support_role_id')
            if support_role_id:
                support_role = guild.get_role(int(support_role_id))
                if support_role:
                    # Remove the support role's view perm; the claimer keeps
                    # access via their personal overwrite (the claimer is a
                    # member of the support role but we add an explicit allow).
                    await channel.set_permissions(
                        support_role,
                        overwrite=discord.PermissionOverwrite(view_channel=False),
                        reason=f'Ticket claimed by {claimer} (hide from other staff)',
                    )
                    await channel.set_permissions(
                        claimer,
                        overwrite=discord.PermissionOverwrite(
                            view_channel=True, send_messages=True,
                            read_message_history=True, attach_files=True,
                            manage_messages=True,
                        ),
                        reason='Claimer access',
                    )
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.claiming] hide_from_other_staff failed: {exc}")

    # 4. Demote support-team perms on claim (read-only) until unclaim.
    if cfg.get('change_support_perms_on_claim'):
        try:
            support_role_id = (panel or {}).get('support_role_id')
            if support_role_id and not cfg.get('hide_from_other_staff'):
                support_role = guild.get_role(int(support_role_id))
                if support_role:
                    await channel.set_permissions(
                        support_role,
                        overwrite=discord.PermissionOverwrite(
                            view_channel=True, send_messages=False,
                            read_message_history=True,
                        ),
                        reason='Claim demoted support to read-only',
                    )
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.claiming] change_support_perms_on_claim failed: {exc}")

    # 5. Custom claimed message.
    msg = cfg.get('claimed_message')
    if msg:
        try:
            embed = discord.Embed(
                title='🔒 Ticket Claimed',
                description=msg.replace('{claim.user}', claimer.mention)
                              .replace('{claim.id}', str(claimer.id)),
                color=discord.Color(0xfaa61a),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f'Claimed by {claimer.display_name}')
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.claiming] send claimed_message failed: {exc}")


async def apply_unclaim_side_effects(
    *,
    bot,
    pdb: PremiumDB,
    channel: discord.TextChannel,
    ticket: Dict,
    panel: Optional[Dict],
    unclaimer: discord.Member,
    ticket_count: Optional[int] = None,
) -> None:
    '''Run AFTER a successful unclaim. Reverses rename / perms / sends msg.'''
    cfg = get_config(pdb, panel.get('panel_id')) if panel else {}
    guild = channel.guild

    # 1. Restore the open-template name.
    if panel:
        restored = naming_mod.compute_unclaimed_name(
            pdb, panel,
            guild={'id': guild.id, 'name': guild.name},
            ticket=ticket,
            ticket_count=ticket_count,
        )
        if restored:
            try:
                await channel.edit(name=_safe_channel_name(restored))
            except discord.HTTPException as exc:
                logging.warning(f"[tickettool.claiming] rename on unclaim failed: {exc}")

    # 2. Restore support-role view perms (reverse hide / demote).
    support_role_id = (panel or {}).get('support_role_id')
    if support_role_id and (cfg.get('hide_from_other_staff') or cfg.get('change_support_perms_on_claim')):
        try:
            support_role = guild.get_role(int(support_role_id))
            if support_role:
                await channel.set_permissions(
                    support_role,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=True, send_messages=True,
                        read_message_history=True, attach_files=True,
                    ),
                    reason='Ticket unclaimed — restored support perms',
                )
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.claiming] restore support perms failed: {exc}")

    # 3. Custom unclaimed message.
    msg = cfg.get('unclaimed_message')
    if msg:
        try:
            embed = discord.Embed(
                title='🔓 Ticket Unclaimed',
                description=msg.replace('{user}', unclaimer.mention)
                              .replace('{claim.user}', unclaimer.mention),
                color=discord.Color(0x57f287),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f'Unclaimed by {unclaimer.display_name}')
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.claiming] send unclaimed_message failed: {exc}")


# =====================================================================
# HELPERS
# =====================================================================

def _safe_channel_name(name: str) -> str:
    import re
    cleaned = ''.join(c if c.isalnum() or c in '-_' else '-' for c in (name or '').lower())
    cleaned = re.sub(r'-+', '-', cleaned).strip('-')
    return (cleaned or 'ticket')[:100]
