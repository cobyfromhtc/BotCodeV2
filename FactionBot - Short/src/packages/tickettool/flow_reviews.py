# -*- coding: utf-8 -*-
'''
TicketTool.flow_reviews — Review/Approval Workflows (Tier 3 Feature #32).

Extends the Support Flow Builder with application-type flows: when a flow
marked as `is_application=1` completes, instead of just creating a normal
ticket, the submission goes into a review queue. Reviewers (a configured
role) can approve or reject each submission.

Workflow:
  1. User completes an application flow.
  2. The submission is stored in flow_review_queue (status='pending').
  3. The bot posts a review message in the configured review_channel_id
     with Approve/Reject buttons.
  4. A reviewer clicks Approve -> the applicant is routed to the
     approved_panel_id panel (a ticket is created there).
  5. A reviewer clicks Reject -> the applicant is notified, optionally
     routed to rejected_panel_id.

Auto-approve / auto-reject timers (optional): if auto_approve_minutes is set,
pending submissions are automatically approved after that time. Same for
auto_reject_minutes. process_due_reviews() runs every minute from
wiring._minute_loop and enforces both deadlines.

Integration:
  * TicketTool.flows._complete_flow -> if flow.is_application, call
    flow_reviews.submit_for_review(...) instead of the normal completion.
  * /reviewpending command -> list pending reviews.
  * ReviewActionView carries persistent Approve/Reject buttons
    (custom_ids `flowrev_approve:<id>` / `flowrev_reject:<id>`); it is
    re-registered after restarts via register_persistent_views(bot), which
    wiring.on_ready_hook calls.
'''

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ui import Button, Modal, TextInput, View

from .db import PremiumDB


# =====================================================================
# UI DESIGN SYSTEM (shared palette)
# =====================================================================
#
# Color palette kept in sync with the rest of the TicketTool UI:
#   success  = green   (0x57F287)  -> approvals, acks
#   error    = red     (0xED4245)  -> rejections, errors
#   warning  = orange  (0xFEE75C)  -> info requests, warnings
#   info     = blurple (0x5865F2)  -> default info
#   gold     = gold    (0xFFD700)  -> pending reviews / applications
REVIEW_COLOR_SUCCESS = 0x57F287
REVIEW_COLOR_ERROR = 0xED4245
REVIEW_COLOR_WARNING = 0xFEE75C
REVIEW_COLOR_INFO = 0x5865F2
REVIEW_COLOR_GOLD = 0xFFD700


# =====================================================================
# EMBED BUILDERS (design-system helpers)
# =====================================================================

def _build_pending_review_embed(*, flow: Dict, applicant: discord.Member,
                                  review_id: str, answers: Dict,
                                  submitted_at: Optional[str]) -> discord.Embed:
    '''Build the rich 'pending review' embed for the review channel.

    Includes applicant info, flow name, submitted time, one field per
    answer, a thumbnail (applicant avatar) and a footer per the design
    system.
    '''
    description = (
        f"**Applicant:** {applicant.mention}\n"
        f"**Flow:** {flow.get('name', 'Unknown')}\n"
    )
    dt = _parse_iso(submitted_at) if submitted_at else None
    if dt is not None:
        description += f"**Submitted:** <t:{int(dt.timestamp())}:R>\n"
    embed = discord.Embed(
        title="📝 Review Pending",
        description=description,
        color=discord.Color(REVIEW_COLOR_GOLD),
        timestamp=datetime.now(timezone.utc),
    )
    # Applicant avatar as thumbnail (visual polish).
    try:
        avatar_url = getattr(getattr(applicant, 'display_avatar', None),
                             'url', None)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
    except Exception:
        pass
    steps = flow.get('steps', []) or []
    if answers:
        for step_id, answer in answers.items():
            step = next((s for s in steps if s.get('id') == step_id), None)
            question = (step or {}).get('question', step_id)
            embed.add_field(
                name=f"❓ {str(question)[:100]}",
                value=str(answer)[:1024] or "_(no answer)_",
                inline=False,
            )
    else:
        embed.add_field(
            name="❓ Answers",
            value="_(no answers recorded)_",
            inline=False,
        )
    embed.set_footer(
        text=f"Application #{review_id} • Submitted by {applicant.display_name}"
    )
    return embed


def _build_decision_embed(*, submission: Dict, approved: bool,
                            reviewer: Optional[discord.Member],
                            panel_id: Optional[str],
                            flow: Optional[Dict] = None) -> discord.Embed:
    '''Build the rich approve / reject confirmation embed.

    Replaces the original pending-review embed after a decision is made.
    Includes the decision, reviewer (or 'auto'), submitted time, optional
    routed-to panel, and a footer with reviewer + decision per the design
    system.
    '''
    flow_name = (flow or {}).get('name', 'Unknown')
    applicant_id = submission.get('applicant_id')
    title = ("✅ Application Approved" if approved
             else "❌ Application Rejected")
    color = (discord.Color(REVIEW_COLOR_SUCCESS) if approved
             else discord.Color(REVIEW_COLOR_ERROR))
    if applicant_id:
        description = (
            f"**Flow:** {flow_name}\n"
            f"**Applicant:** <@{applicant_id}>"
        )
    else:
        description = f"**Flow:** {flow_name}"
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Decision",
        value=("✅ Approved" if approved else "❌ Rejected"),
        inline=True,
    )
    embed.add_field(
        name="Reviewer",
        value=(reviewer.mention if reviewer
               else "⚙️ Auto (deadline elapsed)"),
        inline=True,
    )
    submitted_at = _parse_iso(submission.get('submitted_at'))
    if submitted_at is not None:
        embed.add_field(
            name="Submitted",
            value=f"<t:{int(submitted_at.timestamp())}:R>",
            inline=True,
        )
    if panel_id:
        embed.add_field(
            name="🔀 Routed To",
            value=f"Panel `{panel_id}`",
            inline=False,
        )
    reviewer_name = (reviewer.display_name if reviewer else "auto")
    embed.set_footer(
        text=f"Reviewed by {reviewer_name} • "
             f"Application #{submission.get('review_id')}"
    )
    return embed


class RequestInfoModal(Modal):
    '''Modal for a reviewer to request additional info from an applicant.

    Used by the 'Request Info' button on a review message. The reviewer's
    request is posted to the applicant's ticket channel as a rich warning
    embed, and a note is appended to the review message.
    '''

    def __init__(self, review_id: str):
        super().__init__(title="ℹ️ Request Additional Info")
        self.review_id = review_id
        self.info_input = TextInput(
            label="What information do you need from the applicant?",
            placeholder="e.g., Please provide your invoice number...",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=1,
            max_length=500,
        )
        self.add_item(self.info_input)


# =====================================================================
# CONFIG ACCESSORS
# =====================================================================

def get_config(pdb: PremiumDB, flow_id: str) -> Optional[Dict]:
    return pdb.get_flow_review_config(flow_id)


def save_config(pdb: PremiumDB, *, flow_id: str, guild_id: int,
                review_channel_id: Optional[int] = None,
                reviewer_role_id: Optional[int] = None,
                auto_approve_minutes: int = 0,
                auto_reject_minutes: int = 0,
                approved_panel_id: Optional[str] = None,
                rejected_panel_id: Optional[str] = None) -> None:
    pdb.upsert_flow_review_config({
        'flow_id': flow_id,
        'guild_id': guild_id,
        'review_channel_id': review_channel_id,
        'reviewer_role_id': reviewer_role_id,
        'auto_approve_minutes': auto_approve_minutes,
        'auto_reject_minutes': auto_reject_minutes,
        'approved_panel_id': approved_panel_id,
        'rejected_panel_id': rejected_panel_id,
    })


def is_application_flow(flow: Optional[Dict]) -> bool:
    if not flow:
        return False
    return bool(int(flow.get('is_application', 0) or 0))


def set_flow_application_flag(pdb: PremiumDB, data_manager, flow_id: str,
                                *, is_application: bool) -> bool:
    '''Mark a flow as an application-type flow (routes to review queue).'''
    try:
        flow = pdb.get_flow(flow_id)
        if not flow:
            return False
        # We need to update the is_application column. The upsert_flow method
        # doesn't include this column, so we do a direct UPDATE.
        with pdb.dm._lock:
            cur = pdb._conn.cursor()
            cur.execute(
                'UPDATE ticket_flows SET is_application = ? WHERE flow_id = ?',
                (1 if is_application else 0, flow_id),
            )
            pdb._conn.commit()
        return True
    except Exception as exc:
        logging.warning(f"[flow_reviews] set_flow_application_flag failed: {exc}")
        return False


# =====================================================================
# SUBMIT FOR REVIEW
# =====================================================================

async def submit_for_review(*, bot, pdb: PremiumDB, flow: Dict, ticket: Dict,
                             applicant: discord.Member,
                             answers: Dict, channel: discord.TextChannel) -> Optional[str]:
    '''Submit a completed application flow for review.

    Creates the flow_review_queue row, posts a review message with persistent
    Approve / Reject / Request-Info buttons in the configured review channel,
    and notifies the applicant in their ticket channel. Returns the review_id,
    or None when no review config / review channel exists (the caller should
    then fall back to the normal flow-completion path).
    '''
    flow_id = flow.get('flow_id')
    if not flow_id:
        return None
    cfg = pdb.get_flow_review_config(flow_id)
    if not cfg or not cfg.get('review_channel_id'):
        return None
    guild = channel.guild
    review_channel = guild.get_channel(int(cfg['review_channel_id']))
    if review_channel is None:
        return None
    # Create the review submission row.
    review_id = pdb.add_review_submission({
        'flow_id': flow_id,
        'ticket_id': ticket.get('ticket_id'),
        'guild_id': guild.id,
        'applicant_id': applicant.id,
        'answers': answers,
    })
    # Build the rich pending-review embed (applicant info, flow, submitted
    # time, answer fields, gold color, footer with applicant).
    review_submitted_at = datetime.now(timezone.utc).isoformat()
    embed = _build_pending_review_embed(
        flow=flow,
        applicant=applicant,
        review_id=review_id,
        answers=answers,
        submitted_at=review_submitted_at,
    )
    # Mention the reviewer role if configured.
    mention = ''
    if cfg.get('reviewer_role_id'):
        mention = f"<@&{cfg['reviewer_role_id']}> "
    # Persistent Approve/Reject/Request-Info view (stable custom_ids ->
    # survives restarts once re-registered via register_persistent_views).
    view = build_review_view(review_id)
    try:
        msg = await review_channel.send(
            content=f"{mention}📝 **New application pending review** — please handle:",
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.HTTPException as exc:
        logging.warning(f"[flow_reviews] review message send failed: {exc}")
        return None
    # Remember which message carries the buttons, so decisions made outside
    # the buttons (/reviewdecision, auto timers) can update it + clear them.
    try:
        pdb.set_review_message_id(review_id, msg.id)
    except Exception as exc:
        logging.debug(f"[flow_reviews] review message id store failed: {exc}")
    # Notify the applicant with a rich embed.
    try:
        notify_embed = discord.Embed(
            title="⏳ Application Submitted for Review",
            description=(
                f"Your application for **{flow.get('name', 'Unknown')}** "
                f"has been submitted for review by our staff team."
            ),
            color=discord.Color(REVIEW_COLOR_GOLD),
            timestamp=datetime.now(timezone.utc),
        )
        notify_embed.add_field(
            name="Review ID",
            value=f"`{review_id}`",
            inline=False,
        )
        notify_embed.add_field(
            name="What Happens Next",
            value=(
                "• A reviewer will check your application.\n"
                "• You'll be notified here when a decision is made.\n"
                "• If approved, you'll be routed to the next step."
            ),
            inline=False,
        )
        notify_embed.set_footer(
            text=f"Application #{review_id} • Submitted by {applicant.display_name}"
        )
        await channel.send(embed=notify_embed)
    except discord.HTTPException:
        pass
    return review_id


# =====================================================================
# REVIEW VIEW (Approve / Reject / Request-Info buttons)
# =====================================================================

class ReviewActionView(View):
    '''Persistent Approve / Reject / Request-Info buttons for a review.

    The custom_ids are stable (`flowrev_approve:<review_id>` /
    `flowrev_reject:<review_id>` / `flowrev_info:<review_id>`), so the
    buttons keep working after a restart once the view is re-registered via
    register_persistent_views().

    All buttons are disabled the moment a reviewer clicks one (and the
    original message is edited to reflect this) so that double-clicks can't
    fire two decision paths. After the decision is applied, the view is
    replaced with a fully-disabled one so the final state is visually
    obvious.
    '''

    def __init__(self, review_id: str):
        super().__init__(timeout=None)
        self.review_id = review_id
        self._original_message = None  # set on first interaction
        # ✅ Approve (green / success)
        approve_btn = Button(
            label="Approve", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"flowrev_approve:{review_id}",
        )
        approve_btn.callback = self._on_approve
        self.add_item(approve_btn)
        # ❌ Reject (red / danger)
        reject_btn = Button(
            label="Reject", style=discord.ButtonStyle.danger, emoji="❌",
            custom_id=f"flowrev_reject:{review_id}",
        )
        reject_btn.callback = self._on_reject
        self.add_item(reject_btn)
        # ℹ️ Request Info (secondary / blurple)
        info_btn = Button(
            label="Request Info", style=discord.ButtonStyle.secondary,
            emoji="ℹ️",
            custom_id=f"flowrev_info:{review_id}",
        )
        info_btn.callback = self._on_request_info
        self.add_item(info_btn)

    async def _is_reviewer(self, interaction: discord.Interaction) -> Tuple[bool, Optional[str]]:
        '''Staff gate: reviewer_role_id when configured, else Manage Channels.'''
        bot = interaction.client
        pdb = getattr(bot, 'premium_db', None)
        if pdb is None:
            return False, "Review system not initialized."
        perms = getattr(interaction.user, 'guild_permissions', None)
        if perms is None:
            return False, "Reviews can only be handled inside a server."
        if perms.administrator:
            return True, None
        reviewer_role_id = None
        try:
            submission = pdb.get_review_submission(self.review_id)
            if submission:
                cfg = pdb.get_flow_review_config(submission.get('flow_id'))
                if cfg:
                    reviewer_role_id = cfg.get('reviewer_role_id')
        except Exception:
            reviewer_role_id = None
        if reviewer_role_id:
            role_ids = {r.id for r in getattr(interaction.user, 'roles', [])}
            if int(reviewer_role_id) in role_ids:
                return True, None
            return False, f"You need the <@&{reviewer_role_id}> role to review applications."
        if perms.manage_channels:
            return True, None
        return False, ("You need the Manage Channels permission (or the "
                       "configured reviewer role) to review applications.")

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        ok, err = await self._is_reviewer(interaction)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        # Early-out if already decided (prevents double-decide).
        status = await self._already_decided_with(interaction)
        if status:
            await interaction.response.send_message(
                f"⚠️ This application has already been **{status}**.",
                ephemeral=True,
            )
            return
        # Defer so we can disable buttons + process without timing out.
        await interaction.response.defer()
        self._original_message = interaction.message
        await self._disable_view(interaction)
        await _handle_review_decision(interaction, self.review_id, approved=True)

    async def _on_reject(self, interaction: discord.Interaction) -> None:
        ok, err = await self._is_reviewer(interaction)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        status = await self._already_decided_with(interaction)
        if status:
            await interaction.response.send_message(
                f"⚠️ This application has already been **{status}**.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        self._original_message = interaction.message
        await self._disable_view(interaction)
        await _handle_review_decision(interaction, self.review_id, approved=False)

    async def _on_request_info(self, interaction: discord.Interaction) -> None:
        ok, err = await self._is_reviewer(interaction)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        status = await self._already_decided_with(interaction)
        if status:
            await interaction.response.send_message(
                f"⚠️ This application has already been **{status}**.",
                ephemeral=True,
            )
            return
        # Store the original review message so the modal-submit handler
        # can append an 'Info Requested' field to it later.
        self._original_message = interaction.message
        modal = RequestInfoModal(self.review_id)

        async def _on_submit(inter):
            await self._process_info_request(inter, modal.info_input.value)
        modal.on_submit = _on_submit
        await interaction.response.send_modal(modal)

    async def _already_decided_with(self, interaction: discord.Interaction) -> Optional[str]:
        '''Look up the current review status via the interaction's client.'''
        bot = interaction.client
        pdb = getattr(bot, 'premium_db', None)
        if pdb is None:
            return None
        try:
            submission = pdb.get_review_submission(self.review_id)
        except Exception:
            return None
        if not submission:
            return None
        status = submission.get('status')
        if status and status != 'pending':
            return status
        return None

    async def _disable_view(self, interaction: discord.Interaction) -> None:
        '''Disable all buttons and edit the original message in-place.'''
        for item in self.children:
            item.disabled = True
        msg = self._original_message or getattr(interaction, 'message', None)
        if msg is None:
            return
        try:
            await msg.edit(view=self)
        except discord.HTTPException as exc:
            logging.debug(f"[flow_reviews] disable view failed: {exc}")

    async def _process_info_request(self, interaction: discord.Interaction,
                                      request_text: str) -> None:
        '''Process a reviewer's 'request info' submission.

        Notifies the applicant in their ticket channel with a rich warning
        embed, acknowledges the reviewer ephemerally, and appends an
        'Info Requested' field to the review message.
        '''
        bot = interaction.client
        pdb = getattr(bot, 'premium_db', None)
        if pdb is None:
            await interaction.response.send_message(
                "⚠️ Review system not initialized.", ephemeral=True
            )
            return
        submission = pdb.get_review_submission(self.review_id)
        if not submission:
            await interaction.response.send_message(
                "⚠️ Review submission not found.", ephemeral=True
            )
            return
        if submission.get('status') != 'pending':
            await interaction.response.send_message(
                f"⚠️ This application has already been "
                f"**{submission.get('status')}**.",
                ephemeral=True,
            )
            return
        reviewer = interaction.user
        # Notify the applicant in their ticket channel.
        tt = getattr(bot, 'ticket_tool', None)
        guild = (bot.get_guild(int(submission.get('guild_id') or 0))
                 if bot is not None else None)
        if tt is not None and guild is not None:
            try:
                ticket = tt.data_manager.load_ticket(submission.get('ticket_id'))
                if ticket:
                    ch = guild.get_channel(int(ticket.get('channel_id') or 0))
                    if ch is not None:
                        info_embed = discord.Embed(
                            title="ℹ️ Additional Information Requested",
                            description=(
                                f"{reviewer.mention} has requested additional "
                                f"information regarding your application."
                            ),
                            color=discord.Color(REVIEW_COLOR_WARNING),
                            timestamp=datetime.now(timezone.utc),
                        )
                        info_embed.add_field(
                            name="Request",
                            value=str(request_text)[:1024],
                            inline=False,
                        )
                        info_embed.add_field(
                            name="How to Respond",
                            value=(
                                "Reply in this ticket channel with the "
                                "requested information."
                            ),
                            inline=False,
                        )
                        info_embed.set_footer(
                            text=f"Requested by {reviewer.display_name} "
                                 f"• Review #{self.review_id}"
                        )
                        await ch.send(embed=info_embed)
            except Exception as exc:
                logging.warning(
                    f"[flow_reviews] info request notify failed: {exc}"
                )
        # Acknowledge the reviewer (respond to the modal-submit interaction).
        ack_embed = discord.Embed(
            title="✅ Info Request Sent",
            description="The applicant has been notified of your request.",
            color=discord.Color(REVIEW_COLOR_SUCCESS),
            timestamp=datetime.now(timezone.utc),
        )
        ack_embed.set_footer(
            text=f"Requested by {reviewer.display_name} "
                 f"• Review #{self.review_id}"
        )
        try:
            await interaction.response.send_message(
                embed=ack_embed, ephemeral=True
            )
        except discord.HTTPException:
            pass
        # Append an 'Info Requested' field to the review message so other
        # reviewers can see the question (and the applicant's response when
        # they reply).
        msg = (self._original_message
               or getattr(interaction, 'message', None))
        if msg is not None and getattr(msg, 'embeds', None):
            try:
                embed = msg.embeds[0]
                embed.add_field(
                    name="ℹ️ Info Requested",
                    value=(
                        f"{str(request_text)[:900]}\n— *{reviewer.display_name}*"
                    ),
                    inline=False,
                )
                await msg.edit(embed=embed)
            except (discord.HTTPException, AttributeError) as exc:
                logging.debug(
                    f"[flow_reviews] info request embed update failed: {exc}"
                )


def build_review_view(review_id: str) -> ReviewActionView:
    '''Build the wired, persistent Approve / Reject / Request-Info view.'''
    return ReviewActionView(review_id)


def _build_disabled_review_view(review_id: str) -> ReviewActionView:
    '''Build a ReviewActionView with all buttons disabled.

    Used to replace the active view after a decision is made (prevents
    double-decide via the buttons and shows the final disabled state
    visually). The custom_ids are unchanged so the view still matches the
    message for any subsequent interaction cleanup.
    '''
    view = ReviewActionView(review_id)
    for child in view.children:
        child.disabled = True
    return view


def register_persistent_views(bot) -> None:
    '''Re-register persistent review views for every pending review.

    Persistent views (timeout=None) only receive interactions after a restart
    if they are re-added to the client. wiring.on_ready_hook calls this; the
    per-bot registration set makes repeated on_ready fires a no-op.
    '''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None:
        return
    registered = getattr(bot, '_flowrev_registered_views', None)
    if registered is None:
        registered = set()
        bot._flowrev_registered_views = registered
    count = 0
    for guild in list(getattr(bot, 'guilds', []) or []):
        try:
            pending = pdb.list_pending_reviews(guild.id)
        except Exception:
            continue
        for review in pending:
            rid = review.get('review_id')
            if not rid or rid in registered:
                continue
            try:
                bot.add_view(ReviewActionView(rid))
                registered.add(rid)
                count += 1
            except Exception as exc:
                logging.warning(f"[flow_reviews] persistent view registration failed for {rid}: {exc}")
    if count:
        logging.info(f"[flow_reviews] registered {count} persistent review view(s)")


# =====================================================================
# DECISION CORE (shared by buttons, /reviewdecision and auto timers)
# =====================================================================

def _resolve_panel_id(pdb: PremiumDB, review_id: str, approved: bool) -> Optional[str]:
    '''Look up the configured routing panel for a review decision.'''
    try:
        submission = pdb.get_review_submission(review_id)
        if not submission:
            return None
        cfg = pdb.get_flow_review_config(submission.get('flow_id'))
        if not cfg:
            return None
        return cfg.get('approved_panel_id') if approved else cfg.get('rejected_panel_id')
    except Exception:
        return None


def _decision_field_value(approved: bool, actor: Optional[discord.Member]) -> str:
    who = actor.mention if actor else "⚙️ auto-decision (deadline elapsed)"
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return f"{'✅ Approved' if approved else '❌ Rejected'} by {who} at {stamp}"


async def apply_review_decision(*, bot, pdb: PremiumDB, review_id: str,
                                 approved: bool, panel_id: Optional[str] = None,
                                 actor: Optional[discord.Member] = None,
                                 notes: Optional[str] = None) -> Tuple[bool, str]:
    '''Apply an approve/reject decision to a pending review.

    Updates the review status, notifies the applicant in their ticket channel,
    and routes them to the configured panel (approved_panel_id /
    rejected_panel_id) when one is set. `actor` is the reviewing member, or
    None for system (auto-timer) decisions.

    Returns (ok, user-facing message).
    '''
    submission = pdb.get_review_submission(review_id)
    if not submission:
        return False, "Review submission not found."
    if submission.get('status') != 'pending':
        return False, f"This submission has already been {submission.get('status')}."
    if panel_id is None:
        panel_id = _resolve_panel_id(pdb, review_id, approved)
    new_status = 'approved' if approved else 'rejected'
    pdb.update_review_status(review_id, new_status,
                             actor.id if actor else None, notes)
    # Notify the applicant + route to the target panel.
    tt = getattr(bot, 'ticket_tool', None)
    guild = bot.get_guild(int(submission.get('guild_id') or 0)) if bot is not None else None
    if tt is not None and guild is not None:
        ticket = None
        try:
            ticket = tt.data_manager.load_ticket(submission.get('ticket_id'))
        except Exception as exc:
            logging.debug(f"[flow_reviews] load ticket for decision failed: {exc}")
        if ticket:
            ch = guild.get_channel(int(ticket.get('channel_id') or 0))
            if ch:
                who_mention = actor.mention if actor else None
                who_text = (actor.display_name if actor
                            else "the review system (deadline elapsed)")
                # Rich applicant notification embed (per design system).
                notif_embed = discord.Embed(
                    title=("🎉 Application Approved!" if approved
                           else "❌ Application Rejected"),
                    description=(
                        f"Hi! Your application has been "
                        f"**{'approved' if approved else 'rejected'}**"
                        + (f" by {who_mention}." if who_mention
                           else " by the review system (deadline elapsed).")
                    ),
                    color=(discord.Color(REVIEW_COLOR_SUCCESS) if approved
                           else discord.Color(REVIEW_COLOR_ERROR)),
                    timestamp=datetime.now(timezone.utc),
                )
                if approved and panel_id:
                    notif_embed.add_field(
                        name="✨ Next Step",
                        value=(
                            f"You'll be routed to panel `{panel_id}` shortly."
                        ),
                        inline=False,
                    )
                elif not approved:
                    notif_embed.add_field(
                        name="📌 Status",
                        value=(
                            "You may re-apply in the future if eligible."
                        ),
                        inline=False,
                    )
                notif_embed.set_footer(
                    text=f"Application #{review_id} • Reviewed by {who_text}"
                )
                try:
                    await ch.send(embed=notif_embed)
                except discord.HTTPException:
                    pass
                if panel_id:
                    from . import escalation as esc_mod
                    try:
                        await esc_mod.escalate_ticket(
                            bot=bot, pdb=pdb, channel=ch, ticket=ticket,
                            guild=guild, escalated_by=actor,
                            to_panel_id=panel_id,
                            reason=("Application approved" if approved
                                    else "Application rejected"),
                        )
                    except Exception as exc:
                        logging.warning(f"[flow_reviews] escalate after decision failed: {exc}")
    return True, f"Review {new_status}."


async def update_review_message(*, bot, pdb: PremiumDB, review_id: str,
                                 approved: bool,
                                 actor: Optional[discord.Member] = None) -> bool:
    '''Edit the original review message to reflect a decision.

    Replaces the pending-review embed with a rich decision embed (recolor,
    decision section, footer with reviewer + decision) and replaces the
    active buttons with a fully-disabled view. Used by decision paths that
    don't carry the interaction (the /reviewdecision command and the auto
    timers).
    '''
    try:
        submission = pdb.get_review_submission(review_id)
        if not submission or not submission.get('review_message_id'):
            return False
        cfg = pdb.get_flow_review_config(submission.get('flow_id')) or {}
        channel_id = cfg.get('review_channel_id')
        if not channel_id:
            return False
        guild = bot.get_guild(int(submission.get('guild_id') or 0))
        if guild is None:
            return False
        ch = guild.get_channel(int(channel_id))
        if ch is None:
            return False
        msg = await ch.fetch_message(int(submission['review_message_id']))
        # Build a fresh rich decision embed.
        flow = None
        try:
            flow = pdb.get_flow(submission.get('flow_id'))
        except Exception:
            flow = None
        panel_id = _resolve_panel_id(pdb, review_id, approved)
        embed = _build_decision_embed(
            submission=submission,
            approved=approved,
            reviewer=actor,
            panel_id=panel_id,
            flow=flow,
        )
        # Preserve the answer fields from the original pending-review embed
        # (anything starting with the ❓ prefix). Skip the decision / info
        # fields since the new embed has its own decision section.
        if msg.embeds:
            for f in msg.embeds[0].fields:
                if f.name.startswith("❓"):
                    embed.add_field(
                        name=f.name, value=f.value, inline=f.inline,
                    )
        # Replace the view with a fully-disabled one (no active buttons).
        disabled_view = _build_disabled_review_view(review_id)
        await msg.edit(embed=embed, view=disabled_view)
        return True
    except (discord.HTTPException, ValueError, TypeError) as exc:
        logging.debug(f"[flow_reviews] review message update failed: {exc}")
        return False


async def _handle_review_decision(interaction: discord.Interaction,
                                    review_id: str, *, approved: bool,
                                    panel_id: Optional[str] = None) -> None:
    '''Button entrypoint: apply the decision, then update the review message.

    Assumes the caller has already deferred the interaction (so we use
    interaction.message.edit + interaction.followup.send rather than
    interaction.response.*).
    '''
    bot = interaction.client
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None:
        await interaction.followup.send(
            "⚠️ Review system not initialized.", ephemeral=True
        )
        return
    submission = pdb.get_review_submission(review_id)
    if not submission:
        await interaction.followup.send(
            "⚠️ Review submission not found.", ephemeral=True
        )
        return
    if submission.get('status') != 'pending':
        await interaction.followup.send(
            f"⚠️ This application has already been "
            f"**{submission.get('status')}**.",
            ephemeral=True,
        )
        return
    if panel_id is None:
        panel_id = _resolve_panel_id(pdb, review_id, approved)
    flow = None
    try:
        flow = pdb.get_flow(submission.get('flow_id'))
    except Exception:
        flow = None
    ok, msg = await apply_review_decision(
        bot=bot, pdb=pdb, review_id=review_id, approved=approved,
        panel_id=panel_id, actor=interaction.user,
    )
    if not ok:
        await interaction.followup.send(msg, ephemeral=True)
        return
    # Build the rich decision embed and preserve the answer fields.
    embed = _build_decision_embed(
        submission=submission,
        approved=approved,
        reviewer=interaction.user,
        panel_id=panel_id,
        flow=flow,
    )
    if interaction.message is not None and getattr(interaction.message, 'embeds', None):
        for f in interaction.message.embeds[0].fields:
            if f.name.startswith("❓"):
                embed.add_field(
                    name=f.name, value=f.value, inline=f.inline,
                )
    # Replace the view with a fully-disabled one (final state visible).
    disabled_view = _build_disabled_review_view(review_id)
    try:
        await interaction.message.edit(embed=embed, view=disabled_view)
    except discord.HTTPException as exc:
        logging.debug(
            f"[flow_reviews] review message edit failed: {exc}"
        )
    # Send ephemeral ack to the reviewer.
    ack_emoji = "✅" if approved else "❌"
    ack_word = "approved" if approved else "rejected"
    try:
        await interaction.followup.send(
            f"{ack_emoji} Application **{ack_word}** successfully.",
            ephemeral=True,
        )
    except discord.HTTPException:
        pass


# =====================================================================
# AUTO APPROVE / REJECT TIMER PROCESSING
# =====================================================================

def _parse_iso(ts) -> Optional[datetime]:
    '''Defensively parse an ISO timestamp into an aware UTC datetime.'''
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def process_due_reviews(bot, pdb: PremiumDB) -> None:
    '''Auto-approve/reject pending reviews whose configured deadline passed.

    Runs every minute from wiring._minute_loop. Per-review failures are
    logged and never abort the loop.
    '''
    for guild in list(getattr(bot, 'guilds', []) or []):
        try:
            pending = pdb.list_pending_reviews(guild.id)
        except Exception as exc:
            logging.warning(f"[flow_reviews] list pending reviews failed for guild {guild.id}: {exc}")
            continue
        for review in pending:
            try:
                cfg = pdb.get_flow_review_config(review.get('flow_id'))
                if not cfg:
                    continue
                try:
                    approve_min = int(cfg.get('auto_approve_minutes') or 0)
                    reject_min = int(cfg.get('auto_reject_minutes') or 0)
                except (TypeError, ValueError):
                    continue
                if approve_min <= 0 and reject_min <= 0:
                    continue
                submitted = _parse_iso(review.get('submitted_at'))
                if submitted is None:
                    continue
                now = datetime.now(timezone.utc)
                approved = None
                if approve_min > 0 and now >= submitted + timedelta(minutes=approve_min):
                    approved = True
                elif reject_min > 0 and now >= submitted + timedelta(minutes=reject_min):
                    approved = False
                if approved is None:
                    continue
                panel_id = (cfg.get('approved_panel_id') if approved
                            else cfg.get('rejected_panel_id'))
                ok, _msg = await apply_review_decision(
                    bot=bot, pdb=pdb, review_id=review.get('review_id'),
                    approved=approved, panel_id=panel_id, actor=None,
                    notes="Auto-decision: review deadline elapsed",
                )
                if ok:
                    logging.info(f"[flow_reviews] review {review.get('review_id')} "
                                 f"auto-{'approved' if approved else 'rejected'} (deadline elapsed)")
                    await update_review_message(bot=bot, pdb=pdb,
                                                review_id=review.get('review_id'),
                                                approved=approved, actor=None)
            except Exception as exc:
                logging.warning(f"[flow_reviews] process review {review.get('review_id')} failed: {exc}")


# =====================================================================
# LIST PENDING REVIEWS
# =====================================================================

def list_pending(pdb: PremiumDB, guild_id: int) -> List[Dict]:
    return pdb.list_pending_reviews(guild_id)


def build_pending_embed(reviews: List[Dict]) -> discord.Embed:
    '''Build the rich 'pending reviews' list embed (per the design system).'''
    embed = discord.Embed(
        title="📝 Pending Application Reviews",
        color=discord.Color(REVIEW_COLOR_GOLD),
        timestamp=datetime.now(timezone.utc),
    )
    if not reviews:
        embed.description = ("✅ No pending reviews. "
                             "Great job, team!")
        embed.color = discord.Color(REVIEW_COLOR_SUCCESS)
        embed.set_footer(text="No pending reviews")
        return embed
    for r in reviews[:15]:
        submitted = _parse_iso(r.get('submitted_at'))
        submitted_str = (f"<t:{int(submitted.timestamp())}:R>"
                          if submitted else "Unknown")
        embed.add_field(
            name=f"📋 `{r['review_id']}`",
            value=(
                f"**Applicant:** <@{r['applicant_id']}>\n"
                f"**Submitted:** {submitted_str}\n"
                f"**Flow:** `{r.get('flow_id')}`"
            ),
            inline=False,
        )
    embed.set_footer(
        text=f"{len(reviews)} pending review(s) • Awaiting decision"
    )
    return embed
