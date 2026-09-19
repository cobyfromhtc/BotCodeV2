# -*- coding: utf-8 -*-
'''
TicketTool.flows — Support Flow Builder (Tier 2 Feature #30).

A branching-question flow engine: instead of a flat list of questions, a flow
can branch based on previous answers, route the ticket to a different panel,
and assign a role based on the path taken.

Flow structure (stored as JSON in ticket_flows.steps):
  [
    {id: "s1", question: "What do you need help with?", type: "choice",
     choices: ["Billing","Technical","Other"],
     next: {"Billing":"s2","Technical":"s3","Other":"s4"}},
    {id: "s2", question: "Invoice number?", type: "text",
     default_next: "s5"},
    {id: "s3", question: "Describe the issue", type: "paragraph",
     default_next: "s5"},
    {id: "s4", question: "Briefly describe your request", type: "paragraph",
     default_next: "s5"},
    {id: "s5", question: "Anything else?", type: "text", required: false,
     route_to_panel: "general-support", default_next: null}
  ]

Per-panel config:
  ticket_panels.flow_id — the flow to run when a ticket is created in this panel.

Integration:
  * TicketTool.wiring.on_ticket_create -> flows.start_flow(...)
    posts the first question and records the flow state.
  * A dedicated view (FlowStepView) collects each answer, advances the state,
    and posts the next question. When the flow completes, the answers are
    saved as ticket_answers rows and the welcome message is sent.

This is the pure-discord.py implementation of Ticket Tool's "advanced support
flows" — no visual builder (that would need a website), but the full branching
engine is here.
'''

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput, Select

from .db import PremiumDB, _json_loads_list, _json_loads_dict


# =====================================================================
# UI DESIGN SYSTEM (shared palette + emoji set)
# =====================================================================
#
# Color palette used everywhere in this module (kept in sync with the
# rest of the TicketTool UI):
#   success  = green   (0x57F287)  -> acks, completions
#   error    = red     (0xED4245)  -> errors / rejections
#   warning  = orange  (0xFEE75C)  -> warnings / cancellations
#   info     = blurple (0x5865F2)  -> default step / info
#   gold     = gold    (0xFFD700)  -> reviews / applications
FLOW_COLOR_INFO = 0x5865F2      # blurple (info / default)
FLOW_COLOR_SUCCESS = 0x57F287    # green (success acks)
FLOW_COLOR_WARNING = 0xFEE75C    # orange (warnings / cancellations)
FLOW_COLOR_ERROR = 0xED4245      # red (errors)
FLOW_COLOR_GOLD = 0xFFD700       # gold (reviews / applications)

# Numeric / letter emojis for choice Select options (Select allows max 25).
# 0-9 use number emojis, 10-24 use letter emojis for clarity.
_FLOW_NUM_EMOJIS = [
    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
    "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
    "🇦", "🇧", "🇨", "🇩", "🇪",
    "🇫", "🇬", "🇭", "🇮", "🇯",
    "🇰", "🇱", "🇲", "🇳", "🇴",
]


# =====================================================================
# FLOW CRUD
# =====================================================================

def list_flows(pdb: PremiumDB, guild_id: int) -> List[Dict]:
    return pdb.list_flows(guild_id)


def get_flow(pdb: PremiumDB, flow_id: str) -> Optional[Dict]:
    return pdb.get_flow(flow_id)


def create_or_update_flow(pdb: PremiumDB, *, guild_id: int, name: str,
                           description: Optional[str], steps: List[Dict],
                           start_step_id: Optional[str] = None,
                           flow_id: Optional[str] = None) -> str:
    # Validate step structure.
    for step in steps:
        if 'id' not in step or 'question' not in step:
            raise ValueError("Each step needs an 'id' and 'question'")
        if step.get('type') not in ('text', 'paragraph', 'choice'):
            raise ValueError(f"Invalid step type: {step.get('type')}")
    # Auto-detect start step.
    if not start_step_id:
        start_step_id = steps[0]['id'] if steps else None
    return pdb.upsert_flow({
        'flow_id': flow_id,
        'guild_id': guild_id,
        'name': name,
        'description': description,
        'steps': steps,
        'start_step_id': start_step_id,
    })


def delete_flow(pdb: PremiumDB, flow_id: str) -> bool:
    return pdb.delete_flow(flow_id)


def attach_flow_to_panel(pdb: PremiumDB, data_manager, panel_id: str,
                          flow_id: Optional[str]) -> bool:
    '''Bind a flow to a panel (panel.flow_id column).'''
    try:
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel:
            return False
        panel['flow_id'] = flow_id
        data_manager.save_ticket_panel(panel)
        return True
    except Exception as exc:
        logging.warning(f"[flows] attach_flow_to_panel failed: {exc}")
        return False


# =====================================================================
# FLOW EXECUTION
# =====================================================================

def get_step(flow: Dict, step_id: str) -> Optional[Dict]:
    for step in flow.get('steps', []):
        if step.get('id') == step_id:
            return step
    return None


async def start_flow(*, bot, pdb: PremiumDB, channel, ticket: Dict,
                      panel: Dict) -> bool:
    '''Begin a flow for a newly-created ticket. Returns True if a flow started.'''
    flow_id = panel.get('flow_id')
    if not flow_id:
        return False
    flow = pdb.get_flow(flow_id)
    if not flow or not flow.get('steps'):
        return False
    start_id = flow.get('start_step_id') or flow['steps'][0].get('id')
    pdb.upsert_flow_state({
        'ticket_id': ticket.get('ticket_id'),
        'flow_id': flow_id,
        'current_step_id': start_id,
        'answers': {},
        'started_at': datetime.now(timezone.utc).isoformat(),
        'completed_at': None,
    })
    await _post_step(channel, flow, start_id,
                      ticket=ticket, answers_so_far={})
    return True


def _build_step_embed(flow: Dict, step: Dict, *,
                       answers_so_far: Optional[Dict] = None,
                       applicant: Optional[discord.Member] = None,
                       ticket_id: Optional[str] = None) -> discord.Embed:
    '''Build the embed shown for a single flow step.

    Applies the design system: leading-emoji title, progress indicator
    (Step N of M), branch info (the path of previous answers), a footer
    with the flow name + applicant, and a UTC timestamp.
    '''
    steps = flow.get('steps', []) or []
    total = len(steps)
    current_idx = next(
        (i for i, s in enumerate(steps) if s.get('id') == step.get('id')), -1
    )
    progress = (f"📍 Step {current_idx + 1} of {total}"
                if current_idx >= 0 and total > 0 else "📍 Step")
    stype = step.get('type', 'text')
    type_hint = {
        'choice': '_Select an option from the menu below._',
        'text': '_Click the button below to type a short answer._',
        'paragraph': '_Click the button below to type a detailed answer._',
    }.get(stype, '')
    description = progress if not type_hint else f"{progress}\n{type_hint}"
    embed = discord.Embed(
        title=f"📝 {step.get('question', 'Question')}",
        description=description,
        color=discord.Color(FLOW_COLOR_INFO),
        timestamp=datetime.now(timezone.utc),
    )
    # Branch info: show the path of previous answers so the user has context.
    if answers_so_far:
        path_lines = []
        for sid, ans in answers_so_far.items():
            prev_step = next((s for s in steps if s.get('id') == sid), None)
            label = (prev_step or {}).get('question', sid)
            path_lines.append(f"• **{str(label)[:40]}** → {str(ans)[:40]}")
        if path_lines:
            embed.add_field(
                name="🛤️ Your Path",
                value="\n".join(path_lines)[:1024],
                inline=False,
            )
    footer_parts = [str(flow.get('name') or 'Flow')]
    if applicant is not None:
        footer_parts.append(applicant.display_name)
    elif ticket_id:
        footer_parts.append(f"Ticket #{ticket_id}")
    embed.set_footer(text=" • ".join(footer_parts))
    return embed


async def _post_step(channel, flow: Dict, step_id: str, *,
                      ticket: Optional[Dict] = None,
                      answers_so_far: Optional[Dict] = None) -> None:
    step = get_step(flow, step_id)
    if not step:
        return
    # Resolve the applicant (for footer context) if a ticket was provided.
    applicant = None
    ticket_id = None
    if ticket:
        ticket_id = ticket.get('ticket_id')
        creator_id = int(ticket.get('creator_id') or 0)
        if creator_id and getattr(channel, 'guild', None):
            try:
                applicant = channel.guild.get_member(creator_id)
            except Exception:
                applicant = None
    embed = _build_step_embed(
        flow, step,
        answers_so_far=answers_so_far,
        applicant=applicant,
        ticket_id=ticket_id,
    )
    view = FlowStepView(flow, step)
    try:
        await channel.send(embed=embed, view=view)
    except discord.HTTPException as exc:
        logging.warning(f"[flows] post step failed: {exc}")


class FlowStepView(View):
    '''View that collects the answer to a single flow step.

    For 'choice' steps, renders a Select menu (with numeric / letter
    emojis on each option). For 'text' / 'paragraph' steps, renders a
    primary-styled button that opens a Modal.

    All interactive items are disabled the moment the user submits an
    answer (and the original message is edited to reflect this) so that
    double-clicks cannot re-record / overwrite the answer for the same
    step. A state-level guard in `_submit` also refuses to record an
    answer for a step that is no longer the current one.
    '''

    def __init__(self, flow: Dict, step: Dict):
        super().__init__(timeout=600)
        self.flow = flow
        self.step = step
        self._original_message = None  # set on first interaction
        self.clear_items()
        stype = step.get('type', 'text')
        if stype == 'choice' and step.get('choices'):
            options = []
            for i, c in enumerate(step.get('choices', [])[:25]):
                emoji = (_FLOW_NUM_EMOJIS[i]
                         if i < len(_FLOW_NUM_EMOJIS) else "➡️")
                options.append(discord.SelectOption(
                    label=str(c)[:100],
                    value=str(c)[:100],
                    emoji=emoji,
                ))
            select = Select(
                placeholder="🤔 Select an option...",
                options=options,
            )
            select.callback = self._on_select
            self.add_item(select)
        else:
            emoji = "💬" if stype == 'paragraph' else "✏️"
            label = ("Provide Answer" if stype == 'paragraph'
                     else "Answer Question")
            btn = Button(
                label=label,
                style=discord.ButtonStyle.primary,
                emoji=emoji,
            )
            btn.callback = self._on_button
            self.add_item(btn)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self._original_message = interaction.message
        value = interaction.data['values'][0]
        await self._submit(interaction, value)

    async def _on_button(self, interaction: discord.Interaction) -> None:
        self._original_message = interaction.message
        stype = self.step.get('type', 'text')
        modal = FlowAnswerModal(self.step.get('question', 'Answer'),
                                  paragraph=(stype == 'paragraph'))

        async def _on_submit(inter):
            await self._submit(inter, modal.answer_input.value)
        modal.on_submit = _on_submit
        await interaction.response.send_modal(modal)

    async def _submit(self, interaction: discord.Interaction,
                      answer: str) -> None:
        # Record the answer + advance.
        from . import wiring as W  # noqa: F401  (keeps import order stable)
        bot = interaction.client
        pdb = getattr(bot, 'premium_db', None)
        if pdb is None:
            await interaction.response.send_message(
                "⚠️ Flow state unavailable.", ephemeral=True
            )
            return
        ticket_tool = getattr(bot, 'ticket_tool', None)
        if ticket_tool is None:
            return
        ticket = await ticket_tool.data_manager.async_load_ticket_by_channel(
            interaction.channel.id
        )
        if not ticket:
            await interaction.response.send_message(
                "⚠️ Not a ticket channel.", ephemeral=True
            )
            return
        state = pdb.get_flow_state(ticket['ticket_id'])
        if not state:
            await interaction.response.send_message(
                "⚠️ Flow state lost. Please contact staff.", ephemeral=True
            )
            return
        # Double-answer guard: refuse to record an answer for a step that
        # is no longer the current one (e.g. user re-clicked an old button).
        if state.get('current_step_id') != self.step['id']:
            await interaction.response.send_message(
                "⚠️ This step has already been answered. "
                "Please respond to the latest question.",
                ephemeral=True,
            )
            return
        answers = state.get('answers') or {}
        answers[self.step['id']] = answer
        # Determine next step.
        next_id = None
        if self.step.get('next') and answer in self.step['next']:
            next_id = self.step['next'][answer]
        else:
            next_id = self.step.get('default_next')
        state['current_step_id'] = next_id
        state['answers'] = answers
        pdb.upsert_flow_state(state)
        # Disable all items in-memory so the upcoming message edit prevents
        # any further clicks while we process + post the next step.
        for item in self.children:
            item.disabled = True
        # Ack the user with a rich confirmation embed.
        ack_embed = discord.Embed(
            title="✅ Answer Recorded",
            description=f"**Your answer:** {str(answer)[:200]}",
            color=discord.Color(FLOW_COLOR_SUCCESS),
            timestamp=datetime.now(timezone.utc),
        )
        ack_embed.set_footer(
            text=f"Step: {str(self.step.get('question', ''))[:40]}"
        )
        try:
            await interaction.response.send_message(
                embed=ack_embed, ephemeral=True
            )
        except discord.HTTPException:
            pass
        # Apply the disabled state to the original step message so the
        # buttons / select can't be clicked again.
        msg = self._original_message or getattr(interaction, 'message', None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except discord.HTTPException:
                pass
        if not next_id:
            # Flow complete.
            await _complete_flow(bot, pdb, interaction.channel, ticket, state)
        else:
            await _post_step(
                interaction.channel, self.flow, next_id,
                ticket=ticket, answers_so_far=answers,
            )


class FlowAnswerModal(Modal):
    '''Modal for collecting a text / paragraph answer to a flow step.

    Uses a clear label (the step question itself), a helpful placeholder,
    and a sensible max_length (2000 for paragraph, 200 for short text) per
    the design system. The title carries a leading emoji so the modal is
    visually consistent with the rest of the flow UI.
    '''

    def __init__(self, question: str, *, paragraph: bool = False):
        title_prefix = "💬" if paragraph else "✏️"
        title = f"{title_prefix} {question}"[:45]
        super().__init__(title=title)
        max_len = 2000 if paragraph else 200
        self.answer_input = TextInput(
            label=str(question)[:45],
            placeholder=(
                "Type your detailed answer here..." if paragraph
                else "Type a brief answer..."
            ),
            style=(discord.TextStyle.paragraph if paragraph
                   else discord.TextStyle.short),
            required=True,
            min_length=1,
            max_length=max_len,
        )
        self.add_item(self.answer_input)


async def _complete_flow(bot, pdb: PremiumDB, channel, ticket: Dict, state: Dict) -> None:
    '''Save flow answers as ticket_answers + post completion message.

    Application flows (is_application=1) are diverted into the review queue
    via flow_reviews.submit_for_review instead of the normal completion.
    '''
    flow = pdb.get_flow(state['flow_id'])
    if flow:
        # Persist each answer as a ticket_answers row.
        import uuid as _uuid
        for step_id, answer_text in (state.get('answers') or {}).items():
            try:
                await bot.ticket_tool.data_manager.async_save_ticket_answer({
                    'answer_id': str(_uuid.uuid4())[:8],
                    'ticket_id': ticket['ticket_id'],
                    'question_id': f"flow-{step_id}",
                    'user_id': ticket.get('creator_id'),
                    'answer_text': answer_text,
                    'answered_at': datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                logging.warning(f"[flows] save answer failed: {exc}")

    # === Tier 3: application flows go to the review queue ===
    # submit_for_review() posts the review message (Approve/Reject buttons),
    # stores the submission, and already notifies the applicant — so we return
    # early and skip the normal completion message. On any exception (or when
    # no review config exists) we fall through to normal completion.
    if flow:
        try:
            from . import flow_reviews as fr_mod
            if fr_mod.is_application_flow(flow):
                applicant = channel.guild.get_member(int(ticket.get('creator_id') or 0))
                if applicant is not None:
                    review_id = await fr_mod.submit_for_review(
                        bot=bot, pdb=pdb, flow=flow, ticket=ticket,
                        applicant=applicant,
                        answers=state.get('answers') or {},
                        channel=channel,
                    )
                    if review_id:
                        state['completed_at'] = datetime.now(timezone.utc).isoformat()
                        pdb.upsert_flow_state(state)
                        return
        except Exception as exc:
            logging.warning(f"[flows] application review submission failed: {exc}")

    # Check the last step for routing.
    last_step = None
    if flow and state.get('current_step_id'):
        last_step = get_step(flow, state['current_step_id']) or None
    if last_step and last_step.get('route_to_panel'):
        # Route the ticket to a different panel.
        from . import escalation as esc_mod
        target = last_step['route_to_panel']
        await esc_mod.escalate_ticket(
            bot=bot, pdb=pdb, channel=channel, ticket=ticket,
            guild=channel.guild, escalated_by=None,
            to_panel_id=target, reason="Flow routing",
        )
    # Mark flow complete.
    state['completed_at'] = datetime.now(timezone.utc).isoformat()
    pdb.upsert_flow_state(state)
    try:
        flow_name = (flow or {}).get('name', 'Flow')
        complete_embed = discord.Embed(
            title="✅ Flow Complete",
            description=(
                "All questions answered. "
                "A staff member will be with you shortly."
            ),
            color=discord.Color(FLOW_COLOR_SUCCESS),
            timestamp=datetime.now(timezone.utc),
        )
        complete_embed.set_footer(
            text=f"{flow_name} • Ticket #{ticket.get('ticket_id', '?')}"
        )
        await channel.send(embed=complete_embed)
    except discord.HTTPException:
        pass
