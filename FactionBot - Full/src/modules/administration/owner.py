# -*- coding: utf-8 -*-
"""Owner tools — OWS settings panel + first-startup tutorial."""

# stdlib + discord.py
import discord
import logging
import os
from datetime import datetime, timezone
from discord.ext import commands
from discord.ui import Button, View
from typing import List, Optional

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.helpers import brand_text
from core.ows import OWSToggle, OWS_CATEGORIES, OWS_TOGGLES, ows_bulk_set_category, ows_get, ows_set
from utils.ui.embeds import EmbedBuilder




# ------------------------------------------------------------------
# FIRST-STARTUP OWNER TUTORIAL
# On the very first launch the bot DMs the application owner a complete
# setup tutorial. A flag file (data/.tutorial_sent) guards it so it only
# fires once. The !tutorial command re-sends it.
# ------------------------------------------------------------------
TUTORIAL_FLAG_FILE = os.path.join(config.data_dir, ".tutorial_sent")


def build_tutorial_embeds() -> List[discord.Embed]:
    """Build the multi-page setup tutorial sent to the owner."""
    gn = brand_text("[GANG NAME]")
    ga = brand_text("[GANG ABBR]")
    embeds: List[discord.Embed] = []

    # --- Page 1: Welcome & Prerequisites ---
    e1 = discord.Embed(
        title=f"🚀 Welcome to Your {gn} Bot — Setup Tutorial",
        description=(
            f"Hello! This is a **one-time** setup tutorial to get your **{gn}** gang bot fully running.\n\n"
            "This guide walks you through every step: branding, channels, roles, verification, and final checks.\n\n"
            "💬 **Tip:** Run `!tutorial` at any time to see this guide again."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    e1.add_field(
        name="✅ Prerequisites (verify these first)",
        value=(
            "1. **Bot invited with both** `bot` **and** `applications.commands` **scopes**\n"
            "   (re-invite with the URL containing `scope=bot applications.commands`)\n"
            "2. **Privileged Gateway Intents enabled** in the Discord Developer Portal:\n"
            "   • Server Members Intent  • Message Content Intent  • Presence Intent\n"
            "3. **Bot has Administrator** (or equivalent) permissions in your server."
        ),
        inline=False,
    )
    e1.add_field(
        name="📚 What this tutorial covers",
        value="`Step 1` Branding  •  `Step 2` Channels (`!channelsetup`)  •  `Step 3` Cross-Server Rules  •  `Step 4` Roles (`!rolesetup`)  •  `Step 5` Final Checks  •  `Page 7` New & Notable (Servers / Timing / Limits setup)",
        inline=False,
    )
    e1.set_footer(text="Page 1/7 • First-time setup tutorial")
    embeds.append(e1)

    # --- Page 2: Branding ---
    e2 = discord.Embed(
        title="🎨 Step 1 — Branding",
        description=(
            f"Set your gang identity. Everything the bot says is auto-rebranded from the legacy names to your configured **{gn}** / **{ga}**.\n\n"
            "Run these commands in any channel the bot can see:"
        ),
        color=discord.Color.blurple(),
    )
    e2.add_field(
        name="Set your gang full name",
        value='`!gangname "Brothers Till Death"`\nStops being `[GANG NAME]` once set.',
        inline=False,
    )
    e2.add_field(
        name="Set your gang abbreviation",
        value="`!abrev BTD`\nStops being `[GANG ABBR]` once set (auto-uppercased).",
        inline=False,
    )
    e2.add_field(
        name="Set the bot status",
        value="`!botstatus`\nShows the current status. Edit `config.bot_status` to change what the bot is \"Watching\".",
        inline=False,
    )
    e2.set_footer(text="Page 2/7 • Branding")
    embeds.append(e2)

    # --- Page 3: In-Server Channels ---
    e3 = discord.Embed(
        title="📋 Step 2 — In-Server Channel Setup",
        description=(
            "Run **`!channelsetup`** for an interactive dropdown menu, OR assign each channel individually with **`!setchannel <type> #channel`**.\n\n"
            "Every channel below MUST be assigned. The names below tell you **exactly** what each one is used for:"
        ),
        color=discord.Color.blurple(),
    )
    e3.add_field(
        name="🟢 Member-facing channels",
        value=(
            "• **Invite Tracker Channel** — tracks invite usage / who invited whom\n"
            "• **Welcome / New-Member Channel** — where new members are greeted\n"
            "• **Server Rules Display Channel** — where your server rules are posted\n"
            "• **Verification Panel Channel** — where the verification button/embed lives (members click here to apply)\n"
            "• **Giveaways Posting Channel** — where giveaways are posted"
        ),
        inline=False,
    )
    e3.add_field(
        name="🟡 Staff / logging channels",
        value=(
            "• **Verification Applications Channel** — where submitted applications are posted for staff review\n"
            "• **User Reports Channel** — where `!report` submissions are sent\n"
            "• **Mod Actions & Broadcasts Log Channel** — logs auto-bans AND sends scheduled broadcast/recruitment messages\n"
            "• **Auto-Blacklist Scan Log Channel** — logs periodic blacklist-scan results"
        ),
        inline=False,
    )
    e3.add_field(
        name="🔴 Tickets system",
        value=(
            "• **Tickets Category** — a Discord **CATEGORY** (not a text channel) where ticket channels are created\n"
            "• **Ticket Transcripts Archive Channel** — where closed-ticket transcripts are saved"
        ),
        inline=False,
    )
    e3.set_footer(text="Page 3/7 • In-Server Channels")
    embeds.append(e3)

    # --- Page 4: Cross-Server Rules Sources ---
    e4 = discord.Embed(
        title="🌐 Step 3 — Cross-Server Rules Sources",
        description=(
            "The bot can display rules from **TWO** Discord servers (the bot must be a member of both).\n"
            "Assign these in `!channelsetup` or with `!setchannel`:"
        ),
        color=discord.Color.blurple(),
    )
    e4.add_field(
        name="Gang (In-Game) Rules Source Channel",
        value=(
            "Located in **your gang server**. The bot fetches the rules message from this channel automatically.\n"
            "• Refresh with `!updategangrules`\n"
            "• Set manually with `!setgangrules <text>`"
        ),
        inline=False,
    )
    e4.add_field(
        name="Game Server Rules Source Channel",
        value=(
            "Located in the **game server**. The bot fetches the rules message here.\n"
            "• Refresh with `!updateserverrules`\n"
            "• Set manually with `!setserverrules <text>`"
        ),
        inline=False,
    )
    e4.set_footer(text="Page 4/7 • Cross-Server Rules")
    embeds.append(e4)

    # --- Page 5: Roles ---
    e5 = discord.Embed(
        title="👥 Step 4 — Roles & Setup Commands",
        description=(
            "The bot references several role IDs for verification, permissions, and moderation.\n"
            "Configure them interactively with **`!rolesetup`** (requires Manage Roles).\n\n"
            "To find your Role IDs easily, run:\n"
            "`!getallroles` or `!getallroles` (Owner only) — Lists all roles and their IDs in a single embed with a Copy List button. Auto-updates when roles change."
        ),
        color=discord.Color.orange(),
    )
    e5.add_field(
        name="Roles used by the bot",
        value=(
            "• **Member** — given to accepted/verified members\n"
            "• **Staff** — moderator/staff role (gated commands)\n"
            "• **Verified** — base verified role\n"
            "• **Muted** — applied by the `!mute` / `!tempmute` commands\n"
            "• **Ticket Support** — who can manage/close tickets\n"
            "• **Server Tester** — tester-only features\n"
            "• **Verification Ping** — pinged when a new application is submitted\n"
            "• **Invite Manager** — invite-management features"
        ),
        inline=False,
    )
    e5.add_field(
        name="🛠️ All interactive setup commands",
        value=(
            "Every config the bot uses can now be set from Discord — no file editing required:\n"
            "• **`!channelsetup`** — channels & categories (Manage Channels)\n"
            "• **`!rolesetup`** — role IDs (Manage Roles)\n"
            "• **`!serversetup`** — gang / game server IDs (Manage Guild)\n"
            "• **`!timingsetup`** — intervals & timeouts (Manage Guild)\n"
            "• **`!limitssetup`** — max tickets, poll options, warnings, etc. (Manage Guild)\n\n"
            "All values persist to the database and reload on restart. _(Old `!csetup` still works as an alias.)_"
        ),
        inline=False,
    )
    e5.set_footer(text="Page 5/7 • Roles & Setup Commands")
    embeds.append(e5)

    # --- Page 6: Final Steps ---
    e6 = discord.Embed(
        title="✅ Step 5 — You're All Set!",
        description="Final checks to confirm everything is running smoothly.",
        color=discord.Color.green(),
    )
    e6.add_field(
        name="Verify it worked",
        value="Type `!ping` in your server — the bot should reply with its latency.",
        inline=False,
    )
    e6.add_field(
        name="See all commands",
        value="`!cmds`\nLists every command the bot offers.",
        inline=False,
    )
    e6.add_field(
        name="Need this tutorial again?",
        value="`!tutorial`\nRe-sends this guide to your DMs anytime.",
        inline=False,
    )
    e6.set_footer(text=f"Page 6/7 • You're all set! Welcome to {gn}. 🎉")
    embeds.append(e6)

    # --- Page 7: New & Notable Features (today's changes) ---
    e7 = discord.Embed(
        title="🆕 New & Notable Features — What Changed Recently",
        description=(
            "A quick rundown of the most recent improvements so you don't miss them. "
            "Most of these are transparent — they just work better behind the scenes."
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    e7.add_field(
        name="🧹 `!purge [amount] [-del]` — command message cleanup",
        value=(
            "`!purge 5` deletes 5 messages as before.\n"
            "`!purge 5 -del` (or `!purge amount:5 delete_command:True`) ALSO deletes the `!purge` command message itself and skips the 'Deleted 5 messages.' reply — and the chained-command cleanup prompt is skipped because there's nothing to clean up.\n\n"
            "_Bug fix: purged messages now also appear in the `!msglog` channel — previously they were silently skipped because Discord's bulk-delete doesn't fire on_message_delete._"
        ),
        inline=False,
    )
    e7.add_field(
        name="📋 `!getallroles` — Copy List + auto-updates",
        value=(
            "Owner-only, single embed with all roles & IDs (no pagination).\n"
            "• **Copy List** button — sends the full list to your DMs (ephemeral) for easy copy-paste.\n"
            "• **Auto-updates** — when a role is created, deleted, or renamed, every active `!getallroles` embed in that server refreshes itself.\n"
            "• **Survives restarts** — the button + auto-update still work after the bot comes back online."
        ),
        inline=False,
    )
    e7.add_field(
        name="📨 Invite system — View Previous Invites",
        value=(
            "`!regenerateinvites` now archives the outgoing batch before minting new ones. Click the **📜 View Previous Invites** button on the invite panel to browse old batches, paginated (Previous / Next / Close)."
        ),
        inline=False,
    )
    e7.add_field(
        name="🎫 Ticket system — persistence & race fixes",
        value=(
            "• Ticket panel + control buttons now survive bot restarts (per-panel unique custom_ids).\n"
            "• Claim is atomic (two staff clicking at once can't both win).\n"
            "• Close orders transcript → status update → channel delete, so a crash mid-close no longer leaves a half-closed ticket.\n"
            "• Transcripts pull the entire channel (no more 500-message cap) and escape all HTML so they can't be XSS'd.\n"
            "• Staff notes are actually private (no longer posted to the ticket channel the creator can read)."
        ),
        inline=False,
    )
    e7.add_field(
        name="📊 `!poll` — now hybrid",
        value=(
            "`!poll` and `!poll` both work. Options can be space-separated (`Red Blue Green`) or pipe-separated (`Red|Blue|Green`)."
        ),
        inline=False,
    )
    e7.add_field(
        name="🗄️ Database — giveaways + dbcleanup",
        value=(
            "• Active giveaways now survive bot restarts (loaded from disk instead of reset).\n"
            "• `!dbcleanup` preserves closed-ticket metadata by default (opt-in flag to purge) so historical records aren't accidentally destroyed."
        ),
        inline=False,
    )
    e7.set_footer(text=f"Page 7/7 • {gn} — keeping the streets clean.")
    embeds.append(e7)

    return embeds


async def send_owner_tutorial(force: bool = False) -> None:
    """
    DM the bot application owner the full setup tutorial.

    Fires automatically once on first startup (guarded by a flag file).
    Pass force=True to re-send regardless of the flag (used by !tutorial).
    """
    try:
        if not force and os.path.exists(TUTORIAL_FLAG_FILE):
            return

        # Fetch the application owner from Discord.
        app_info = await state.bot.application_info()
        owner = app_info.owner
        if owner is None:
            logging.warning("[Tutorial] Could not resolve bot owner; skipping tutorial DM.")
            return

        embeds = build_tutorial_embeds()
        sent = 0
        for embed in embeds:
            try:
                await owner.send(embed=embed)
                sent += 1
            except discord.Forbidden:
                logging.warning("[Tutorial] Owner has DMs closed; cannot send tutorial page %d.", sent + 1)
                break
            except discord.HTTPException as exc:
                logging.warning("[Tutorial] Failed to send tutorial page %d: %s", sent + 1, exc)
                break

        if sent > 0:
            logging.info(f"[Tutorial] Sent {sent}/{len(embeds)} tutorial pages to owner {owner}.")
            print(f"[Tutorial] Sent {sent}/{len(embeds)} tutorial pages to owner {owner}.")
            # Mark as sent so it does not fire again on next startup.
            try:
                with open(TUTORIAL_FLAG_FILE, "w", encoding="utf-8") as fh:
                    fh.write(datetime.now(timezone.utc).isoformat())
            except Exception as exc:
                logging.warning(f"[Tutorial] Could not write flag file: {exc}")
        else:
            logging.warning("[Tutorial] No tutorial pages were delivered. Owner DMs may be closed.")
            print("[Tutorial] WARNING: Could not DM the owner. Run !tutorial in a server to retry.")
    except Exception as exc:
        logging.exception("[Tutorial] Failed to send owner tutorial: %s", exc)
        print(f"[Tutorial] ERROR: {exc}")

class OwnerSettingsView(View):
    MAX_TOGGLE_BUTTONS = 15

    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.current_category: str = OWS_CATEGORIES[0]
        self.message: Optional[discord.Message] = None
        self._build_components()

    def _build_components(self) -> None:
        self.clear_items()
        cat_select = discord.ui.Select(
            placeholder="📋 Select a category to configure…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=cat,
                    value=cat,
                    description=f"{sum(1 for t in OWS_TOGGLES if t.category == cat)} toggle(s)",
                )
                for cat in OWS_CATEGORIES
            ],
            row=0,
        )
        cat_select.callback = self._on_category_selected
        self.add_item(cat_select)

        toggles = self._toggles_for_category()
        for idx, t in enumerate(toggles[:self.MAX_TOGGLE_BUTTONS]):
            row = 1 + (idx // 5)
            if row > 3:
                break
            enabled = ows_get(t.key)
            icon = "✅" if enabled else "❌"
            label = f"{icon} {t.label}"[:80]
            style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
            btn = Button(label=label, style=style, row=row)
            btn.callback = self._make_toggle_callback(t.key)
            self.add_item(btn)

        enable_all = Button(label="Enable All", style=discord.ButtonStyle.success, row=4, emoji="🔓")
        enable_all.callback = self._on_enable_all
        self.add_item(enable_all)

        disable_all = Button(label="Disable All", style=discord.ButtonStyle.secondary, row=4, emoji="🔒")
        disable_all.callback = self._on_disable_all
        self.add_item(disable_all)

        close_btn = Button(label="Close", style=discord.ButtonStyle.danger, row=4, emoji="✖")
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    def _toggles_for_category(self) -> List[OWSToggle]:
        return [t for t in OWS_TOGGLES if t.category == self.current_category]

    def _make_toggle_callback(self, key: str):
        async def _callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
                return
            new_val = not ows_get(key)
            ows_set(key, new_val)
            logging.info(f"[OWS] {interaction.user} toggled '{key}' → {'ON' if new_val else 'OFF'}")
            self._build_components()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return _callback

    async def _on_category_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        self.current_category = self.category_select_placeholder_values(interaction)
        self._build_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)
        # Save the new category so it survives a restart
        data_manager.save_ows_panel_state(self.message.id, self.message.channel.id, self.author_id, self.current_category)

    def category_select_placeholder_values(self, interaction: discord.Interaction) -> str:
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                if child.values:
                    return child.values[0]
        return self.current_category

    async def _on_enable_all(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        count = ows_bulk_set_category(self.current_category, True)
        logging.info(f"[OWS] {interaction.user} enabled all {count} toggles in '{self.current_category}'")
        self._build_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_disable_all(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        count = ows_bulk_set_category(self.current_category, False)
        logging.info(f"[OWS] {interaction.user} disabled all {count} toggles in '{self.current_category}'")
        self._build_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        self.stop()
        data_manager.delete_ows_panel_state() # Clean up DB on close
        try:
            await interaction.message.delete()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            for child in self.children:
                child.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass

    def _build_embed(self) -> discord.Embed:
        toggles = self._toggles_for_category()
        enabled_count = sum(1 for t in toggles if ows_get(t.key))
        total = len(toggles)

        embed = discord.Embed(
            title=f"⚙️ Owner Settings — {self.current_category}",
            description=(
                f"**{enabled_count}/{total}** features enabled in this category.\n"
                f"Click a button to flip it. Every change is saved to the database instantly.\n"
            ),
            color=discord.Color.blurple() if enabled_count >= total // 2 else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        lines: List[str] = []
        for t in toggles:
            val = ows_get(t.key)
            icon = "🟢" if val else "🔴"
            lines.append(f"{icon} **{t.label}**\n   ↳ {t.description}")

        chunk_size = 4
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            field_num = (i // chunk_size) + 1
            field_name = "Features" if field_num == 1 else f"Features (cont. {i + 1}–{i + len(chunk)})"
            embed.add_field(name=field_name, value="\n".join(chunk), inline=False)

        pct = (enabled_count / total * 100) if total else 0
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        embed.add_field(
            name="📊 Category Progress",
            value=f"`{bar}` **{pct:.0f}%** ({enabled_count}/{total})",
            inline=False,
        )

        all_enabled = sum(1 for t in OWS_TOGGLES if ows_get(t.key))
        embed.set_footer(
            text=f"Overall: {all_enabled}/{len(OWS_TOGGLES)} features enabled • "
                 f"Use the dropdown to switch categories • Owner-only"
        )
        return embed

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        data_manager.delete_ows_panel_state() # Clean up DB on timeout
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""

    @bot.command(name="ows", aliases=["OWS", "Ows", "ownerws", "ownersettings"])
    @commands.is_owner()
    async def ows_cmd(ctx: commands.Context) -> None:
        """Open the Owner Settings panel — toggle any bot feature on or off."""
        view = OwnerSettingsView(ctx.author.id)
        embed = view._build_embed()
        view.message = await ctx.send(embed=embed, view=view)
        # Save the panel state to the database
        data_manager.save_ows_panel_state(view.message.id, ctx.channel.id, ctx.author.id, view.current_category)
        logging.info(f"[OWS] Owner settings panel opened by {ctx.author}")


    # ------------------------------------------------------------------
    # Owner-only command to re-send the setup tutorial on demand.
    # ------------------------------------------------------------------
    @bot.command(name="tutorial", description="Re-send the bot setup tutorial to your DMs (owner only)")
    @commands.is_owner()
    async def tutorial_cmd(ctx: commands.Context) -> None:
        """Re-send the full setup tutorial to the owner's DMs."""
        await ctx.send(embed=EmbedBuilder.success(
            "Tutorial Sent",
            "Check your DMs — the full setup tutorial is on its way.\n"
            "_(If you didn't receive it, your DMs may be closed. Enable DMs from server members and try again.)_"
        ), ephemeral=True)
        await send_owner_tutorial(force=True)
        logging.info(f"[Tutorial] Manually re-sent by owner {ctx.author}")
