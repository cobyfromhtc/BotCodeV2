# -*- coding: utf-8 -*-
"""Info — userinfo/serverinfo/ping/uptime, paginated help, role list."""

# stdlib + discord.py
import discord
import logging
from datetime import datetime, timezone
from discord.ext import commands
from discord.ui import Button, View
from typing import List, Tuple

from core import EDITION, __version__
from core.state import config, data_manager
from core.helpers import brand_text, get_uptime
from utils.ui.embeds import EmbedBuilder
from modules.runtime.events import _chain_offsets




class PaginatedHelpView(View):
    """Paginated view for the `!cmds` help command.

    Shows one page at a time with First / Previous / page-indicator / Next /
    Last / Close buttons. Only the user who invoked the command can navigate.
    The page indicator is a disabled button that always shows "Page N / M".
    """

    def __init__(self, user_id: int, pages: List[discord.Embed], timeout: float = 300, initial_page: int = 0):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.pages = pages

        # Clamp the initial page to make sure it doesn't exceed the number of pages we have
        max_page = len(pages) - 1 if pages else 0
        self.current_page = min(initial_page, max_page) if max_page > 0 else 0

        self._update_buttons()

    def _update_buttons(self) -> None:
        self.first_button.disabled = self.current_page == 0
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.pages) - 1
        self.last_button.disabled = self.current_page >= len(self.pages) - 1
        # Page indicator reflects the current position.
        self.page_indicator.label = f"📄 {self.current_page + 1}/{len(self.pages)}"

    def current_embed(self) -> discord.Embed:
        embed = self.pages[self.current_page]
        # Update the footer to reflect the current page number.
        embed.set_footer(text=(
            f"Page {self.current_page + 1}/{len(self.pages)} • "
            f"{brand_text('[GANG ABBR]')} Commands • FactionBot {EDITION} v{__version__}"
        ))
        return embed

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Menu", "Only the person who ran `!cmds` can navigate these pages."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="⏮ First", style=discord.ButtonStyle.secondary, custom_id="help_first_page")
    async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        if self.current_page > 0:
            self.current_page = 0
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="⬅ Previous", style=discord.ButtonStyle.primary, custom_id="help_prev_page")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="📄 1/1", style=discord.ButtonStyle.secondary, custom_id="help_page_indicator", disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Disabled button — no interaction should reach here, but stay safe.
        await interaction.response.defer()

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.primary, custom_id="help_next_page")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Last ⏭", style=discord.ButtonStyle.secondary, custom_id="help_last_page")
    async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        last = len(self.pages) - 1
        if self.current_page < last:
            self.current_page = last
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, custom_id="help_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        self.stop()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        """Disable all buttons when the view times out."""
        for child in self.children:
            child.disabled = True


def build_help_pages() -> List[discord.Embed]:
    """Build the paginated help embeds for the `!cmds` command.

    All commands are PREFIX-ONLY (`!`). The list is kept in sync with the
    actual registered commands (see the `!cmds` command). Sections are grouped
    into pages where each page's total text stays under ~1800 chars (well
    within Discord's 4096-char embed description limit). Categories are never
    split across pages.
    """
    # Each section: (category_title, list_of_command_lines)
    # Every command uses the `!` prefix — the bot is now prefix-only.
    sections: List[Tuple[str, List[str]]] = [
        ("🛡️ Moderation", [
            "`!kick @user1 @user2 [reason]` - Kick one or multiple users",
            "`!ban @user1 @user2 [reason]` - Ban one or multiple users",
            "`!banid <user_id> [reason]` - Ban a user by ID",
            "`!softban @user [reason]` - Softban (kick + delete messages)",
            "`!mute @user [reason]` - Mute a user",
            "`!unmute @user` - Unmute a user",
            "`!tempmute @user [seconds] [reason]` - Temporarily mute",
            "`!warn @user <reason>` - Warn a member",
            "`!warnings [@user]` - View warnings for a member",
            "`!clearwarnings @user` - Clear a member's warnings",
            "`!purge [amount] [-del]` - Delete messages (empty = all). `-del` removes the command message too",
            "`!purgeall` - Alias of `!purge` (purge entire channel)",
            "`!nopurge [id1, id2, id3]` - Protect message(s) from purge (verification channel only)",
            "`!lock` - Lock the current channel",
            "`!unlock` - Unlock the current channel",
            "`!slowmode [seconds]` - Set slowmode on the current channel",
        ]),
        ("🚫 Blacklist System", [
            "`!blacklist <keyword>` - Add a keyword to the blacklist",
            "`!unblacklist <keyword>` - Remove a keyword from the blacklist",
            "`!blacklistlist` - Show all blacklisted keywords",
            "`!blacklistscan` - Scan all members for blacklisted keywords",
            "`!checkprofile [@user]` - Check a user's profile for blacklisted keywords",
        ]),
        ("🎫 Tickets — Panels & Creation", [
            "`!panel` - Create a new ticket panel (interactive builder)",
            "`!panels` - List all ticket panels",
            "`!deletepanel <panel_id>` - Delete a ticket panel",
            "`!panelupdate` - Refresh an existing panel message",
            "`!panelquestion` - Manage the form questions shown before ticket creation",
            "`!multipanel` - Combine up to 25 panels into ONE message",
            "`!dropdownpanel` - Create a dropdown-style panel (select menu)",
            "`!reactionpanel` - Create a reaction-based ticket panel",
            "`!new` - Open a new ticket (command-style)",
            "`!ticket` - Alias of `!new`",
            "`!tickets` - View all open tickets",
        ]),
        ("🎫 Tickets — Management", [
            "`!claim` - Claim the current ticket (race-free)",
            "`!unclaim` - Release your claim",
            "`!close [reason]` - Close the current ticket (rating-first for creators)",
            "`!closerequest` (`!ca`) - Request staff to close this ticket",
            "`!add @user|@role` - Add a user or role to the current ticket",
            "`!remove @user|@role` - Remove a user or role from the ticket",
            "`!rename <name>` - Rename the ticket channel",
            "`!move <panel_id>` - Move the ticket to a different panel/category",
            "`!note <text>` - Add a private staff note",
            "`!notes` - View all staff notes for this ticket",
            "`!priority <low|normal|high|urgent>` - Set ticket priority",
            "`!pause [30m|1h|2d|1w|indefinite]` - Pause ALL automations for this ticket",
            "`!resume` - Resume automations for a paused ticket",
            "`!private` - Make this ticket private (hidden from other staff)",
            "`!unprivate` - Restore staff access to a private ticket",
            "`!rate` - Send the rating (CSAT) prompt to the ticket creator",
            "`!transcript [channel] [lines]` - Generate a transcript (full history)",
            "`!reopen <ticket_id>` - Reopen a closed ticket",
        ]),
        ("🎫 Tickets — Info, Settings & Cleanup", [
            "`!ticket-info` - Full status embed for this ticket (priority, claim, SLA, escalations)",
            "`!ticketstats` - View ticket statistics for this server",
            "`!ticketdebug` - Ticket system diagnostics",
            "`!permissionlevel` - Show your ticket-system permission level",
            "`!tickethelp` - Show every ticket-system command by category",
            "`!ticketsettings` - Configure the ticket system",
            "`!ticketlog` - Configure the ticket log channel and events",
            "`!ticketblacklist @user [reason]` - Block a user from creating tickets",
            "`!ticketunblacklist @user` - Remove a user from the ticket blacklist",
            "`!tcategory` - Manage ticket categories (internal folders)",
            "`!setcategory <category_id>` - Change this ticket's category (staff)",
            "`!limitbypass` - Set roles that bypass ticket limits for a panel",
            "`!dbcleanup` - Scan and remove stale/invalid database entries",
            "",
            "_(Ticket buttons + panels survive bot restarts; claim/close are race-free)_",
        ]),
        ("⚙️ Ticket Tool — Automations & Custom Commands", [
            "`!automate <panel_id> <name> <trigger> [actions_json]` - Create/update automation rule",
            "`!automatelist <panel_id>` - List all automations for a panel",
            "`!automatedelete <automation_id>` - Delete an automation rule",
            "`!customcommand <name> <actions_json> [description]` - Create a custom !command",
            "`!customcommandlist` - List all custom commands",
            "`!customcommandremove <name>` - Remove a custom command",
            "`!roleauto <panel_id> <event> [add_roles] [remove_roles]` - Role automation",
            "",
            "_(Triggers: created, closed, reopened, owner_left, close_request, claim, unclaim, delayed, no_response)_",
        ]),
        ("📝 Ticket Tool — Flows, Reviews & KB", [
            "`!flow <name> <steps_json>` - Create/update a branching support flow",
            "`!flowlist` - List all flows",
            "`!flowattach <panel_id> [flow_id]` - Attach/detach flow to panel",
            "`!flowdelete <flow_id>` - Delete a flow",
            "`!flowapplication <flow_id> <true|false>` - Mark flow as application (review queue)",
            "`!flowreviewconfig <flow_id> <review_channel> [reviewer_role]` - Configure review settings",
            "`!reviewpending` - List pending application reviews",
            "`!reviewdecision <review_id> <approve|reject> [notes]` - Approve/reject a review",
            "`!kb add <title> <content> [category]` - Add a KB article",
            "`!kb view <article_id>` - View an article",
            "`!kb list [category]` - List articles",
            "`!kb search <query>` - Search the knowledge base",
            "`!kb remove <article_id>` - Remove an article",
            "`!kb stats` - KB view-count statistics",
        ]),
        ("🏷️ Ticket Tool — Naming, Embeds & Scheduling", [
            "`!naming <panel_id> [open_template] [padding]` - Configure naming + number padding",
            "`!panelembed <panel_id> <embed_json>` - Add a multi-embed to a panel (up to 10)",
            "`!panelembedlist <panel_id>` - List panel embeds",
            "`!panelembedremove <embed_id>` - Remove a panel embed",
            "`!panelembedenable <panel_id> <true|false>` - Enable/disable multi-embed mode",
            "`!schedule <panel_id> <timezone> <periods_json>` - Configure business hours",
            "`!schedview <panel_id>` - View a panel's schedule",
            "",
            "_(Variables: {ticket.count} {ticket.user} {claim.user} |pad:4 |lower |ago + 25 more)_",
        ]),
        ("🔒 Ticket Tool — Claiming, Threads & Replies", [
            "`!claimconfig <panel_id> [options]` - Advanced claiming config",
            "`!staffthread <panel_id> <true|false>` - Private staff discussion threads",
            "`!threadtickets <panel_id> <true|false> [parent_channel]` - Thread-based tickets",
            "`!channelrecycle <panel_id> <true|false>` - Recycle channels (avoids 500 limit)",
            "`!brandedreplies <panel_id> <setup|disable|view>` - Anonymous/branded staff replies",
            "`!modmessage <panel_id> <event> [content] [embeds] [buttons]` - Moderator message config",
            "`!modmessagelist <panel_id>` - List moderator messages",
            "`!modmessageremove <message_id>` - Remove a moderator message",
        ]),
        ("⏱️ Ticket Tool — SLA & Escalation", [
            "`!slaconfig [first_response_hours] [resolution_hours] [escalation_role]` - Configure SLA targets",
            "`!slareport` - View SLA performance statistics",
            "`!escalate [to_panel_id] [reason]` - Escalate the current ticket",
            "`!escalateroute <from_panel> <to_panel> [notify_role]` - Configure escalation route",
            "`!escalationhistory` - View escalation history for the current ticket",
        ]),
        ("📊 Ticket Tool — Analytics & CSAT", [
            "`!analytics [days]` - Ticket analytics overview (trends, by-panel, by-priority)",
            "`!csat [by]` - CSAT report (staff/panel/time, pos/neg %, feedback)",
            "`!staffstats` - Per-staff performance (claimed, closed, avg rating, response time)",
            "`!staffanalytics [days]` - Advanced staff analytics (p50/p90, peak hours, activity)",
            "`!ticktrends [days]` - Long-term ticket trend report (90-day capacity planning)",
            "`!responsedistribution` - First-response time distribution buckets",
            "`!export` - Export all tickets as CSV",
        ]),
        ("🌍 Ticket Tool — Localization & Transcripts", [
            "`!locale <language> [timezone]` - Set guild language (en/es/fr/de)",
            "`!localestring <key> <value>` - Override a specific string",
            "`!localelist` - List all localizable strings",
            "`!transcriptconfig [save_mode] [auto_dm] [custom_message]` - Transcript automation",
            "`!transcriptconfig2 [disable_html] [save_on_delete] [format]` - Extended transcript config",
        ]),
        ("🎉 Giveaways & 📊 Leveling", [
            "`!giveaway <hours> <winners> <prize>` - Create a giveaway",
            "`!endgiveaway <id>` - End a giveaway early",
            "`!level [@user]` - View your (or another's) level card",
            "`!leaderboard` - View the server leaderboard",
            "`!messageson` / `!messagesoff` - Toggle XP gain from your messages",
        ]),
        ("📋 Rules Management", [
            "`!updategangrules` - Fetch & cache the gang rules from a channel message",
            "`!updateserverrules` - Fetch & cache the server rules from a channel message",
            "`!setgangrules <text>` - Manually set gang rules (posts a formatted embed)",
            "`!setserverrules <text>` - Manually set server rules (posts a formatted embed)",
            "`!viewcachedrules` - View the currently cached rules status",
        ]),
        ("👥 Roles", [
            "`!addrole @user [role_name]` - Add a role to a user",
            "`!removerole @user @role` - Remove a role from a user",
            "`!roleall @role` - Give a role to all members",
            "`!getallroles` (`!roles`) - List all roles & IDs with a Copy List button (owner only)",
        ]),
        ("🔐 Verification", [
            "`!verify` - Start the verification process",
            "`!verifyuser @user` - Manually verify a user",
            "`!securitycheck @user` - Run a security check on a user",
        ]),
        ("📨 Invites", [
            "`!setupinvites` - Set up invite tracking (posts panel + history button)",
            "`!checkinvites` - View invite status (rich embed + history button)",
            "`!inviteinfo [code]` - View detailed info about a specific invite",
            "`!regenerateinvites` - Regenerate all invites (archives the old batch first)",
            "",
            "_(Old invite batches are archived — click 'View Previous Invites' on the panel to browse them, paginated)_",
        ]),
        ("ℹ️ Information", [
            "`!userinfo @user` - User information",
            "`!serverinfo` - Server information",
            "`!membercount` - Member count",
            "`!ping` - Bot latency",
            "`!uptime_cmd` - Bot uptime",
            "`!botstatus` - Check active processes",
            "`!cmds` - Show this help menu (paginated)",
        ]),
        ("⭐ Premium Features (FREE Carl-bot / Dyno clones)", [
            "`!rr add #channel <msg_id> <emoji> <@role> [mode]` - Add reaction role",
            "`!rr list` - List all reaction roles (up to 250)",
            "`!rr remove <mapping_id>` - Remove one mapping",
            "`!rr clear <msg_id>` - Clear all mappings on a message",
            "`!rr creator <msg_id>` - Quick modal creator",
            "`!stickyrole enable` - Turn ON sticky roles (re-apply on rejoin)",
            "`!stickyrole add @role` - Mark a role as sticky-eligible",
            "`!stickyrole list` / `!stickyrole status [@member]`",
            "`!msglog enable #channel` - Log edits + deletes with content",
            "`!msglog edits on|off` / `!msglog deletes on|off`",
            "`!msglog ignore #channel` / `!msglog status`",
            "`!botbranding name \"Brothers Till Death\"` - Sets the community name (`[GANG NAME]` stops being the placeholder)",
            "`!botbranding footer <text>` - Custom embed footer",
            "`!botbranding color #RRGGBB` / `thumbnail <url>` / `image <url>`",
            "`!botbranding banner <url>` / `avatar <url>` (owner)",
            "`!botbranding view` / `!botbranding clear`",
        ]),
        ("🛠️ Setup & Configuration", [
            "`!channelsetup` (`!csetup`) - Interactive channel & category setup (Manage Channels)",
            "`!rolesetup` (`!rsetup`) - Interactive role ID setup (Manage Roles)",
            "`!serversetup` (`!ssetup`) - Gang / game server IDs (Manage Guild)",
            "`!timingsetup` (`!tsetup`) - Intervals & timeouts (Manage Guild)",
            "`!limitssetup` (`!lsetup`) - Max tickets, poll options, warnings, etc. (Manage Guild)",
            "`!setchannel <type> #channel` - Quick single-channel setter (Owner)",
            "`!gangname <\"Name\">` - Set gang full name (Owner)",
            "`!abrev ABBR` - Set gang abbreviation (Owner)",
            "`!ows` - Owner settings panel (feature toggles, 60+ switches)",
            "",
            "_(All setup values persist to the database and reload on restart.)_",
        ]),
        ("🤝 Faction Access — License Management", [
            "`!license` - Show this server's license panel (DMs: authority dashboard)",
            "`!license pending` - Guilds waiting for approval",
            "`!license approve <guild> [bundle …]` - Approve a pending guild (+ optional bundles)",
            "`!license deny <guild> [reason]` - Deny a pending guild",
            "`!license suspend <guild> [reason]` - Suspend a license (instant off-switch)",
            "`!license resume <guild>` - Re-enable a suspended license",
            "`!license revoke <guild> [reason]` - Revoke (re-joining needs re-approval)",
            "`!license expiry <guild> <30d|12h|…|off>` - Time-limited license (auto-suspends)",
            "`!license list [all|licensed|pending|suspended|revoked|left|home]` - Guilds by status",
            "`!license info <guild|here|id>` - One guild's full license record",
            "`!license home [guild|here]` - Show / set the home faction guild",
        ]),
        ("🤝 Faction Access — Bundles, Identity & Authority", [
            "`!license grant <guild> <bundle …>` - Grant feature bundles (approve first)",
            "`!license ungrant <guild> <bundle …>` - Take bundles away",
            "`!license catalog` - Every bundle + command count (public)",
            "`!license identity <guild> [tag <t>] [name \"<n>\"] [display \"<d>\"] | reset` - Per-guild identity",
            "`!license authority [add|remove] @user` - License authority allowlist (app owner always qualifies)",
            "`!license invite [guild|id]` - Pre-scoped OAuth invite link",
            "`!license audit [guild] [count]` - Audit trail of every licensing action",
            "`!request <note>` - Allied server leaders: reach the license authority (rate-limited)",
            "",
            "_(Allied factions only get granted bundles — unclassified commands stay home-only; `!license catalog` shows drift)_",
        ]),
        ("📦 Other", [
            "`!report @user` - Report a user",
            "`!poll [duration] [question] [options...]` - Create a poll (space- or pipe-separated options)",
            "`!auditlog [limit]` - View audit logs",
            "`!shutdown` - Gracefully shut down the bot",
            "`!tutorial` - Re-send the setup tutorial to your DMs (owner only)",
            "",
            "**💡 Multi-Command Chaining:**",
            "Run multiple commands in one message separated by commas!",
            "Example: `!cmds, !setupinvites, !regenerateinvites`",
            "*(Staff get a prompt to clean up the command message — except with `!purge ... -del`, which deletes itself)*",
        ]),
    ]

    # Group sections into pages. Each page is a list of (title, lines).
    # We keep each page under MAX_PAGE_CHARS so the embed description stays
    # well within Discord's limits.
    MAX_PAGE_CHARS = 1800
    pages_sections: List[List[Tuple[str, List[str]]]] = []
    current: List[Tuple[str, List[str]]] = []
    current_len = 0

    for title, lines in sections:
        # Render this section to estimate its length.
        rendered = f"**{title}**\n" + "\n".join(lines)
        section_len = len(rendered)

        # If this section alone exceeds the limit, it gets its own page.
        if section_len > MAX_PAGE_CHARS:
            # Flush the current page first.
            if current:
                pages_sections.append(current)
                current = []
                current_len = 0
            pages_sections.append([(title, lines)])
            continue

        # If adding this section would overflow, flush and start a new page.
        if current_len + section_len + 4 > MAX_PAGE_CHARS and current:
            pages_sections.append(current)
            current = []
            current_len = 0

        current.append((title, lines))
        current_len += section_len + 4  # +4 for the blank line between sections

    if current:
        pages_sections.append(current)

    # Build one embed per page.
    embeds: List[discord.Embed] = []
    colors = [
        discord.Color.blurple(),
        discord.Color.green(),
        discord.Color.orange(),
        discord.Color.red(),
        discord.Color.purple(),
        discord.Color.teal(),
    ]
    for idx, page_sections in enumerate(pages_sections):
        description_parts: List[str] = []
        for title, lines in page_sections:
            description_parts.append(f"**{title}**")
            description_parts.extend(lines)
            description_parts.append("")  # blank line between sections
        description = "\n".join(description_parts).strip()

        embed = discord.Embed(
            title=f"📜 {brand_text('[GANG ABBR]')} — Available Commands",
            description=description,
            color=colors[idx % len(colors)],
            timestamp=datetime.now(timezone.utc),
        )
        # Footer is set dynamically by the view, but set a default here too.
        embed.set_footer(text=f"Page {idx + 1}/{len(pages_sections)} • {brand_text('[GANG ABBR]')} Commands")
        embeds.append(embed)

    return embeds
    return embeds


def build_getallroles_embed(guild: discord.Guild) -> discord.Embed:
    """Build the All-Roles embed for a guild.

    Shared between the command, the auto-update handlers, and the startup
    refresh so every code path renders the embed identically. All roles are
    packed into a single embed (auto-split into multiple code blocks if the
    list would exceed Discord's 4096-char description limit). No pagination.
    """
    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)

    lines: List[str] = []
    for role in roles:
        if role.is_default():
            lines.append(f"@everyone - {role.id}")
        else:
            lines.append(f"{role.name} - {role.id}")

    embed = discord.Embed(
        title=f"📋 All Roles in {guild.name}",
        description=f"**{len(roles)} role(s) total**",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    if not roles:
        embed.description = "No roles found in this server."
        return embed

    # Discord embed description max = 4096 chars. Each code block needs
    # ```\n ... \n``` wrappers (8 chars overhead). Pack as many roles as
    # possible into each block, splitting into multiple blocks if needed —
    # but always within a SINGLE embed (no pagination buttons).
    MAX_DESC = 4096
    CODE_FENCE_OVERHEAD = 8

    blocks: List[str] = []
    current_block: List[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len + CODE_FENCE_OVERHEAD > MAX_DESC - len(blocks) * 2 and current_block:
            blocks.append("```\n" + "\n".join(current_block) + "\n```")
            current_block = []
            current_len = 0
        current_block.append(line)
        current_len += line_len

    if current_block:
        blocks.append("```\n" + "\n".join(current_block) + "\n```")

    if len(blocks) == 1:
        embed.description = f"**{len(roles)} role(s) total**\n{blocks[0]}"
    else:
        embed.description = f"**{len(roles)} role(s) total** — split into {len(blocks)} blocks"
        for i, block in enumerate(blocks[:25]):  # Discord max 25 fields
            embed.add_field(
                name=f"Roles (part {i + 1}/{len(blocks)})",
                value=block,
                inline=False,
            )

    embed.set_footer(text="📋 Click Copy List to copy all roles • Auto-updates on role changes")
    return embed


def build_getallroles_plain_text(guild: discord.Guild) -> str:
    """Build a plain-text (no markdown) list of all roles + IDs.

    Used by the Copy List button so the ephemeral response is easy to
    select-all-and-copy. One role per line: `RoleName - ID`.
    """
    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)
    parts: List[str] = []
    for role in roles:
        if role.is_default():
            parts.append(f"@everyone - {role.id}")
        else:
            parts.append(f"{role.name} - {role.id}")
    return "\n".join(parts) if parts else "No roles found."


async def refresh_getallroles_messages(guild: discord.Guild) -> None:
    """Rebuild and edit every active getallroles embed for `guild`.

    Called from on_guild_role_create / on_guild_role_delete /
    on_guild_role_update so the lists stay live. Messages that no longer
    exist are pruned from the tracking table.
    """
    tracked = data_manager.load_getallroles_messages(guild_id=guild.id)
    if not tracked:
        return

    embed = build_getallroles_embed(guild)
    view = GetAllRolesView()

    for row in tracked:
        channel = guild.get_channel(row['channel_id'])
        if channel is None:
            # Channel was deleted — stop tracking this embed.
            data_manager.delete_getallroles_message(row['message_id'])
            continue
        try:
            message = await channel.fetch_message(row['message_id'])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Message was deleted or we lost access — stop tracking it.
            data_manager.delete_getallroles_message(row['message_id'])
            continue
        try:
            await message.edit(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden):
            # Edit failed (e.g. channel now read-only). Leave it tracked;
            # a future role change will retry, or it'll be pruned if the
            # message is gone.
            pass


class GetAllRolesView(View):
    """Persistent view attached to every getallroles embed.

    Carries a single 'Copy List' button that DMs the clicker the full role
    list as plain text in a code block so they can select-all-and-copy.
    Persisted via bot.add_view() on startup so the button keeps working
    after a bot restart.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Copy List", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="getallroles_copy")
    async def copy_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return
        text = build_getallroles_plain_text(interaction.guild)

        # Discord message content limit is 2000 chars. Code fence adds 8.
        # If the list fits in one message, send it as a single code block.
        # Otherwise split across multiple ephemeral followups.
        MAX_MSG = 2000
        CODE_OVERHEAD = 8  # "```\n" + "\n```"
        CHUNK = MAX_MSG - CODE_OVERHEAD  # ~1992 chars per code block

        await interaction.response.defer(ephemeral=True)

        if len(text) <= CHUNK:
            await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)
            return

        # Split on newline boundaries so we never cut a role line in half.
        lines = text.split("\n")
        chunks: List[str] = []
        current: List[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current_len + line_len > CHUNK and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))

        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            header = f"**All roles ({i}/{total})**\n" if total > 1 else ""
            await interaction.followup.send(f"{header}```\n{chunk}\n```", ephemeral=True)

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # --- INFORMATION COMMANDS ---
    @bot.command()
    async def userinfo(ctx: commands.Context, member: discord.Member) -> None:
        embed = discord.Embed(title=f"{member.name}'s Info", color=discord.Color.blue())
        embed.add_field(name="ID", value=member.id)
        embed.add_field(name="Joined", value=member.joined_at)
        embed.add_field(name="Top Role", value=member.top_role)
        await ctx.send(embed=embed)


    @bot.command()
    async def serverinfo(ctx: commands.Context) -> None:
        try:
            guild = ctx.guild
            online = sum(1 for m in guild.members if m.status != discord.Status.offline)
            bots = sum(1 for m in guild.members if m.bot)
            humans = guild.member_count - bots
        
            embed = discord.Embed(title=f"{guild.name} Server Info", color=discord.Color.blurple())
            embed.add_field(name="Server ID", value=guild.id, inline=True)
            embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
            embed.add_field(name="Created", value=guild.created_at.strftime("%B %d, %Y"), inline=True)
            embed.add_field(name=f"Members ({guild.member_count})", value=f"Online: {online}\nOffline: {guild.member_count - online}\nBots: {bots}\nHumans: {humans}", inline=False)
            embed.add_field(name=f"Channels ({len(guild.text_channels) + len(guild.voice_channels)})", value=f"Text: {len(guild.text_channels)}\nVoice: {len(guild.voice_channels)}", inline=True)
            embed.add_field(name="Other Stats", value=f"Roles: {len(guild.roles)}\nBoosts: {guild.premium_subscription_count}", inline=True)
        
            if guild.icon:
                embed.set_thumbnail(url=guild.icon.url)
        
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send("An error occurred")
            logging.error(f"Error in serverinfo: {str(e)}")


    @bot.command()
    async def membercount(ctx: commands.Context) -> None:
        member_role = ctx.guild.get_role(config.roles.member)
        if member_role is None:
            await ctx.send("Member role not found.")
            return
        members_with_role = [m for m in ctx.guild.members if member_role in m.roles]
        await ctx.send(f'There are {len(members_with_role)} members with the member role.')


    @bot.command()
    async def ping(ctx: commands.Context) -> None:
        await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')


    @bot.command()
    async def uptime_cmd(ctx: commands.Context) -> None:
        await ctx.send(f"Bot uptime: {get_uptime()}")

    @bot.command(name="cmds", description="Show all available commands (paginated)")
    async def cmds(ctx: commands.Context) -> None:
        """Shows the paginated help menu."""
        embeds = build_help_pages()
    
        # Check if this command was part of a multi-command chain
        # Tuple format: (page_offset, is_chained)
        initial_page, is_chained = _chain_offsets.pop(ctx.message.id, (0, False))
    
        # If it's chained, send JUST the embed with no buttons
        if is_chained:
            await ctx.send(embed=embeds[initial_page])
            return
        
        # Otherwise, send the normal interactive pagination
        view = PaginatedHelpView(ctx.author.id, embeds, initial_page=initial_page)
        view.message = await ctx.send(embed=view.current_embed(), view=view)


    @bot.command(name="getallroles", aliases=["roles", "listroles"], description="Get a list of all roles and their IDs (owner only)")
    @commands.is_owner()
    @commands.guild_only()
    async def getallroles_cmd(ctx: commands.Context) -> None:
        """Display all roles in the server with their IDs on a single list.

    The embed is auto-updated whenever a role is created, deleted, or renamed
    in this server — no need to re-run the command. A persistent Copy List
    button sends the full list to your DMs (ephemeral) for easy copy-paste.
    The embed and button both survive a bot restart.
    """
        embed = build_getallroles_embed(ctx.guild)
        view = GetAllRolesView()
        message = await ctx.send(embed=embed, view=view)

        # Track the message so role-create/delete/update events can refresh it
        # and on_ready can re-attach the persistent view after a restart.
        try:
            data_manager.save_getallroles_message(message.id, ctx.channel.id, ctx.guild.id, ctx.author.id)
        except Exception as e:
            logging.warning(f"[GetAllRoles] Could not track message {message.id}: {e}")
