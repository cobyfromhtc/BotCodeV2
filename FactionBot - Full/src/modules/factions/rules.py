# -*- coding: utf-8 -*-
"""Rules — gang/server rules embeds, caching, update commands."""

# stdlib + discord.py
import discord
import logging
from datetime import datetime, timezone
from discord.ext import commands
from discord.ui import View
from typing import List, Optional, Tuple

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.helpers import brand_text
from utils.ui.embeds import EmbedBuilder




# --- RULES VIEWS ---
class PaginatedRulesView(View):
    def __init__(self, user_id: int, title: str, emoji: str, color: discord.Color, rules_pages: List[str], timeout: float = 180):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.title = title
        self.emoji = emoji
        self.color = color
        self.rules_pages = rules_pages
        self.current_page = 0
        self._update_buttons()
    
    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.rules_pages) - 1
    
    def _create_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"{self.emoji} {self.title}", description=self.rules_pages[self.current_page], color=self.color)
        embed.set_footer(text=f"Page {self.current_page + 1}/{len(self.rules_pages)}  GNG Verification System")
        embed.timestamp = datetime.now(timezone.utc)
        return embed
    
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="rules_prev_page")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your verification!", ephemeral=True)
            return
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self._create_embed(), view=self)
        else:
            await interaction.response.defer()
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="rules_next_page")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your verification!", ephemeral=True)
            return
        if self.current_page < len(self.rules_pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self._create_embed(), view=self)
        else:
            await interaction.response.defer()


def split_rules_into_pages(rules: str, max_chars: int = 3900) -> List[str]:
    if not rules:
        return ["No rules available. Contact staff."]
    if len(rules) <= max_chars:
        return [rules]
    
    pages: List[str] = []
    current_page = ""
    sections = rules.split('\n---\n')
    
    for section in sections:
        if len(section) > max_chars:
            paragraphs = section.split('\n\n')
            for para in paragraphs:
                if len(current_page) + len(para) + 2 > max_chars:
                    if current_page:
                        pages.append(current_page.strip())
                    current_page = para + "\n\n"
                else:
                    current_page += para + "\n\n"
        else:
            if len(current_page) + len(section) + 10 > max_chars:
                if current_page:
                    pages.append(current_page.strip())
                current_page = section + "\n\n---\n\n"
            else:
                current_page += section + "\n\n---\n\n"
    
    if current_page.strip():
        pages.append(current_page.strip())
    
    return pages if pages else [rules[:max_chars]]


class RulesButtonView(View):
    def __init__(self, user_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.rules_viewed = {'gang': False, 'server': False}
        # Branded button label at instance time (config loaded by setup_hook).
        self.gang_rules_button.label = brand_text("[GANG ABBR] Rules")
    
    def _create_rules_embed(self, title: str, rules: str, emoji: str, color: discord.Color, page_num: int = 1, total_pages: int = 1) -> discord.Embed:
        embed = discord.Embed(title=title, description=rules[:4096] if rules else "No rules available. Contact staff.", color=color)
        if total_pages > 1:
            embed.set_footer(text=f"Page {page_num}/{total_pages}  GNG Verification System")
        else:
            embed.set_footer(text="GNG Verification System")
        embed.timestamp = datetime.now(timezone.utc)
        return embed
    
    @discord.ui.button(label="[GANG ABBR] Rules", style=discord.ButtonStyle.primary, custom_id="gang_rules_button")
    async def gang_rules_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your verification!", ephemeral=True)
            return
        
        await interaction.response.defer(thinking=True)
        rules = await self._fetch_gang_rules()
        pages = split_rules_into_pages(rules)
        
        button.style = discord.ButtonStyle.success
        button.label = brand_text("[GANG ABBR] Rules Viewed")
        self.rules_viewed['gang'] = True
        await interaction.message.edit(view=self)
        
        if len(pages) > 1:
            paginated_view = PaginatedRulesView(self.user_id, brand_text("[GANG ABBR] In-Game Rules"), "", discord.Color.red(), pages)
            await interaction.followup.send(embed=paginated_view._create_embed(), view=paginated_view, ephemeral=True)
        else:
            embed = self._create_rules_embed(brand_text("[GANG ABBR] In-Game Rules"), rules, "", discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="Server Rules", style=discord.ButtonStyle.secondary, custom_id="server_rules_button")
    async def server_rules_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your verification!", ephemeral=True)
            return
        
        await interaction.response.defer(thinking=True)
        rules = await self._fetch_server_rules()
        pages = split_rules_into_pages(rules)
        
        button.style = discord.ButtonStyle.success
        button.label = "Server Rules Viewed"
        self.rules_viewed['server'] = True
        await interaction.message.edit(view=self)
        
        if len(pages) > 1:
            paginated_view = PaginatedRulesView(self.user_id, "Server Game & Server Rules", "", discord.Color.blue(), pages)
            await interaction.followup.send(embed=paginated_view._create_embed(), view=paginated_view, ephemeral=True)
        else:
            embed = self._create_rules_embed("Server Game & Server Rule", rules, "", discord.Color.blue())
            await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def _fetch_gang_rules(self) -> str:
        try:
            channel = state.bot.get_channel(config.servers.gang_rules_channel)
            if channel:
                try:
                    message = await channel.fetch_message(config.servers.gang_rules_message)
                    # Recover text from a plain message OR an embed-only message.
                    rules = message.content or ""
                    if not rules and message.embeds:
                        rules = "\n\n".join((e.description or "") for e in message.embeds)
                    state.rules_cache['gang_rules'] = rules
                    state.rules_cache['gang_last_updated'] = datetime.now(timezone.utc).isoformat()
                    save_rules_cache()
                    return rules
                except discord.NotFound:
                    logging.warning("[Rules] GNG rules message not found, using cache")
                except discord.Forbidden:
                    logging.warning("[Rules] No permission to fetch GNG rules, using cache")
        except Exception as e:
            logging.error(f"[Rules] Error fetching GNG rules: {e}")
        
        if state.rules_cache['gang_rules']:
            return state.rules_cache['gang_rules']
        return "Unable to fetch rules. Please contact a staff member."
    
    async def _fetch_server_rules(self) -> str:
        try:
            server_guild = state.bot.get_guild(config.servers.server_server_id)
            if server_guild:
                channel = server_guild.get_channel(config.servers.server_rules_channel)
                if channel:
                    try:
                        message = await channel.fetch_message(config.servers.server_rules_message)
                        # Recover text from a plain message OR an embed-only message.
                        rules = message.content or ""
                        if not rules and message.embeds:
                            rules = "\n\n".join((e.description or "") for e in message.embeds)
                        state.rules_cache['server_rules'] = rules
                        state.rules_cache['server_last_updated'] = datetime.now(timezone.utc).isoformat()
                        save_rules_cache()
                        return rules
                    except discord.NotFound:
                        logging.warning("[Rules] Server rules message not found, using cache")
                    except discord.Forbidden:
                        logging.warning("[Rules] No permission to fetch Server rules, using cache")
            else:
                logging.warning("[Rules] Bot not in Server server, using cache")
        except Exception as e:
            logging.error(f"[Rules] Error fetching Server rules: {e}")
        
        if state.rules_cache['server_rules']:
            return state.rules_cache['server_rules']
        return "Unable to fetch Server rules. The bot may not be in the Server server.\nPlease contact a staff member or check the Server server directly."


def load_rules_cache() -> None:
    try:
        state.rules_cache = data_manager.load_rules_cache()
        logging.info("[RulesCache] Loaded cached rules from SQLite")
    except Exception as e:
        logging.error(f"[RulesCache] Error loading: {e}")


def save_rules_cache() -> None:
    try:
        data_manager.save_rules_cache(state.rules_cache)
    except Exception as e:
        logging.error(f"[RulesCache] Error saving: {e}")


# --- RULES EMBED HELPERS ---
# Build a Discohook-style embed (or a small list of paginated embeds) from raw
# rules text. The text goes straight into the embed description, so Discord
# renders Markdown (bold/italic/code/blockquotes/links) exactly the way the
# admin formatted it. Pagination kicks in automatically past Discord's 4096-char
# description limit so long rulesets are never silently truncated.
def _build_rules_embeds(
    title: Optional[str],
    rules_text: str,
    color: discord.Color,
    footer_text: str,
) -> List[discord.Embed]:
    if not rules_text:
        return [discord.Embed(title=title, description="No rules available. Contact staff.", color=color)]
    pages = split_rules_into_pages(rules_text, max_chars=3900)
    # Discord caps a single message at 10 embeds. Cap the pages and fold the
    # overflow into the 10th page so no rules are lost.
    if len(pages) > 10:
        pages = pages[:9] + ["\n\n---\n\n".join(pages[9:])]
    total = len(pages)
    embeds: List[discord.Embed] = []
    for i, page in enumerate(pages, start=1):
        e = discord.Embed(
            title=title if i == 1 else None,  # only the first page shows the title
            description=page,
            color=color,
        )
        e.set_footer(text=f"{footer_text} • Page {i}/{total}" if total > 1 else footer_text)
        e.timestamp = datetime.now(timezone.utc)
        embeds.append(e)
    return embeds


async def _post_or_edit_rules_embed(
    channel: Optional[discord.TextChannel],
    existing_message_id: int,
    title: Optional[str],
    rules_text: str,
    color: discord.Color,
    footer_text: str,
) -> Tuple[Optional[int], str]:
    """Post (or edit) a formatted rules embed in the given channel.

    Returns (message_id, status) where status is:
      'posted'   — a new message was created
      'edited'   — the existing message was updated in place
      'error: …'  — something went wrong (channel missing, no perms, etc.)
    """
    if channel is None:
        return None, "error: Rules channel is not set. Run `!updategangrules #channel <message_id>` or `!channelsetup` first."
    if not isinstance(channel, discord.TextChannel):
        return None, "error: The configured rules channel is not a text channel."

    embeds = _build_rules_embeds(title, rules_text, color, footer_text)

    # Prefer editing the existing rules message so the channel doesn't fill up
    # with a new embed every time the admin refreshes the rules.
    if existing_message_id:
        try:
            message = await channel.fetch_message(existing_message_id)
            await message.edit(embeds=embeds)
            return message.id, "edited"
        except discord.NotFound:
            pass  # old message was deleted — fall through to posting a new one
        except discord.Forbidden:
            return None, "error: No permission to edit messages in the rules channel."
        except Exception as e:
            logging.warning(f"[RulesEmbed] Could not edit existing rules message {existing_message_id}: {e}")

    try:
        message = await channel.send(embeds=embeds)
        return message.id, "posted"
    except discord.Forbidden:
        return None, "error: No permission to send embeds in the rules channel."
    except Exception as e:
        return None, f"error: {e}"

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # --- RULES MANAGEMENT COMMANDS ---
    @bot.command(name="updategangrules", description="Fetch & cache the gang rules from a channel message")
    @commands.has_permissions(administrator=True)
    async def updategangrules_cmd(
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        message_id: Optional[str] = None,
    ) -> None:
        """Fetch the gang rules message and cache it.

    Without arguments: uses the configured gang_rules_channel + gang_rules_message.
    With channel + message_id: fetches that specific message AND persists the
    channel/message IDs so future !updategangrules (no args) re-fetches it.

    Examples:
      !updategangrules                                  # use configured source
      !updategangrules #rules-chat 1539152833226743910  # fetch a specific message
    """
        try:
            # If a channel + message ID were provided, resolve & persist them.
            if channel is not None or message_id:
                if channel is None or not message_id:
                    await ctx.send(brand_text(
                        "Please provide BOTH a channel and a message ID.\n"
                        "Example: `!updategangrules #rules-chat 1539152833226743910`"
                    ))
                    return
                mid = message_id.strip().strip("`")
                # Accept a full Discord message link too: .../channel_id/message_id
                if "/" in mid:
                    mid = mid.split("/")[-1]
                if not mid.isdigit():
                    await ctx.send(brand_text(
                        "`message_id` must be a numeric message ID (or a full Discord message link).\n"
                        "Example: `!updategangrules #rules-chat 1539152833226743910`"
                    ))
                    return
                config.servers.gang_rules_channel = channel.id
                config.servers.gang_rules_message = int(mid)
                config.save_channel_settings()

            target_channel = bot.get_channel(config.servers.gang_rules_channel)
            if not target_channel:
                await ctx.send(brand_text(
                    "[GANG ABBR] rules channel not found.\n"
                    "Set it with `!updategangrules #channel <message_id>` or via `!channelsetup`."
                ))
                return
            message = await target_channel.fetch_message(config.servers.gang_rules_message)
            # Recover the rules text whether the source is a plain message OR an
            # embed-only message (e.g. one posted by /setgangrules). For paginated
            # embeds, concatenate all embed descriptions back together.
            rules_text = message.content or ""
            if not rules_text and message.embeds:
                rules_text = "\n\n".join((e.description or "") for e in message.embeds)
            state.rules_cache['gang_rules'] = rules_text
            state.rules_cache['gang_last_updated'] = datetime.now(timezone.utc).isoformat()
            save_rules_cache()

            embed = EmbedBuilder.success(
                brand_text("[GANG ABBR] Rules Updated"),
                brand_text(
                    f"Successfully cached the [GANG ABBR] rules.\n"
                    f"**Source:** {target_channel.mention}\n"
                    f"**Message ID:** `{config.servers.gang_rules_message}`\n"
                    f"**Characters:** {len(rules_text)}"
                )
            )
            await ctx.send(embed=embed)
            logging.info(f"[RulesCache] Gang rules updated by {ctx.author} (channel={target_channel.id}, msg={config.servers.gang_rules_message})")
        except discord.NotFound:
            await ctx.send(brand_text(
                "[GANG ABBR] rules message not found. Double-check the message ID — "
                "it must be a message that exists in the configured channel."
            ))
        except discord.Forbidden:
            await ctx.send(brand_text("No permission to fetch the [GANG ABBR] rules message."))
        except Exception as e:
            await ctx.send(f"Error: {str(e)}")


    @bot.command(name="updateserverrules", description="Fetch & cache the server rules from a channel message")
    @commands.has_permissions(administrator=True)
    async def updateserverrules_cmd(
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        message_id: Optional[str] = None,
    ) -> None:
        """Fetch the server (VPRP) rules message and cache it.

    Without arguments: uses the configured server_rules_channel + server_rules_message
    (looked up in the server_server_id guild).
    With channel + message_id: fetches that specific message AND persists the
    channel/message IDs so future !updateserverrules (no args) re-fetches it.

    Examples:
      !updateserverrules                                  # use configured source
      !updateserverrules #server-rules 1453140236732469258
    """
        try:
            if channel is not None or message_id:
                if channel is None or not message_id:
                    await ctx.send(
                        "Please provide BOTH a channel and a message ID.\n"
                        "Example: `!updateserverrules #server-rules 1453140236732469258`"
                    )
                    return
                mid = message_id.strip().strip("`")
                if "/" in mid:
                    mid = mid.split("/")[-1]
                if not mid.isdigit():
                    await ctx.send(
                        "`message_id` must be a numeric message ID (or a full Discord message link).\n"
                        "Example: `!updateserverrules #server-rules 1453140236732469258`"
                    )
                    return
                config.servers.server_rules_channel = channel.id
                config.servers.server_rules_message = int(mid)
                config.save_channel_settings()
                target_channel = channel
            else:
                server_guild = bot.get_guild(config.servers.server_server_id)
                if not server_guild:
                    await ctx.send(
                        "Bot is not in the Server server. Cannot fetch rules.\n"
                        "Use `!updateserverrules #channel <message_id>` to fetch from a specific channel."
                    )
                    return
                target_channel = server_guild.get_channel(config.servers.server_rules_channel)
                if not target_channel:
                    await ctx.send(
                        "Server rules channel not found.\n"
                        "Set it with `!updateserverrules #channel <message_id>` or via `!channelsetup`."
                    )
                    return
            message = await target_channel.fetch_message(config.servers.server_rules_message)
            # Recover the rules text whether the source is a plain message OR an
            # embed-only message (e.g. one posted by /setserverrules).
            rules_text = message.content or ""
            if not rules_text and message.embeds:
                rules_text = "\n\n".join((e.description or "") for e in message.embeds)
            state.rules_cache['server_rules'] = rules_text
            state.rules_cache['server_last_updated'] = datetime.now(timezone.utc).isoformat()
            save_rules_cache()

            embed = EmbedBuilder.success(
                "Server Rules Updated",
                f"Successfully cached the Server rules.\n"
                f"**Source:** {target_channel.mention}\n"
                f"**Message ID:** `{config.servers.server_rules_message}`\n"
                f"**Characters:** {len(rules_text)}"
            )
            await ctx.send(embed=embed)
            logging.info(f"[RulesCache] Server rules updated by {ctx.author} (channel={target_channel.id}, msg={config.servers.server_rules_message})")
        except discord.NotFound:
            await ctx.send("Server rules message not found. Double-check the message ID — it must exist in the configured channel.")
        except discord.Forbidden:
            await ctx.send("No permission to fetch the Server rules message.")
        except Exception as e:
            await ctx.send(f"Error: {str(e)}")


    @bot.command(name="setserverrules", description="Set the server rules text & post it as a formatted embed")
    @commands.has_permissions(administrator=True)
    async def setserverrules_cmd(ctx: commands.Context, *, rules: str) -> None:
        # Cache the raw text first so the verification flow always has it, even if
        # posting the public embed fails (no perms, channel missing, etc.).
        state.rules_cache['server_rules'] = rules
        state.rules_cache['server_last_updated'] = datetime.now(timezone.utc).isoformat()
        save_rules_cache()

        # Post (or edit) a Discohook-style embed in the server rules channel so
        # members see a clean, Markdown-rendered rules message. The raw text goes
        # into the embed description; Discord renders bold/italic/code/quotes.
        target_channel = bot.get_channel(config.servers.server_rules_channel)
        posted_id, status = await _post_or_edit_rules_embed(
            channel=target_channel,
            existing_message_id=config.servers.server_rules_message,
            title="📜 Server Rules",
            rules_text=rules,
            color=discord.Color.blue(),
            footer_text="Server Rules • Last updated",
        )
        if posted_id is not None:
            # Persist the channel + message IDs so !updateserverrules (no args)
            # re-fetches this exact embed later.
            config.servers.server_rules_channel = target_channel.id  # type: ignore[union-attr]
            config.servers.server_rules_message = posted_id
            config.save_channel_settings()

        if target_channel is None:
            status_msg = (
                "Server rules **cached** for verification, but the server rules "
                "channel isn't set, so no public embed was posted.\n"
                "Set it with `!updateserverrules #channel <message_id>` or `!channelsetup`, "
                "then re-run `!setserverrules` to publish the embed."
            )
        elif posted_id is None:
            status_msg = f"Server rules **cached** for verification, but the public embed couldn't be posted:\n{status}"
        else:
            action = "edited" if status == "edited" else "posted a new"
            status_msg = (
                f"Server rules **cached** and the public rules embed was **{action}** "
                f"in {target_channel.mention}.\n"
                f"**Characters:** {len(rules)}"
            )

        await ctx.send(embed=EmbedBuilder.success("Server Rules Set", status_msg))
        logging.info(f"[RulesCache] Server rules manually set by {ctx.author} (embed status: {status})")


    @bot.command(name="setgangrules", description="Set the gang rules text & post it as a formatted embed")
    @commands.has_permissions(administrator=True)
    async def setgangrules_cmd(ctx: commands.Context, *, rules: str) -> None:
        # Guard against a very common mistake: passing a message ID (or a Discord
        # message link) instead of the actual rules text. A raw message ID would
        # be cached as the literal string of digits, and members would see that
        # number when they open the rules.
        stripped = rules.strip().strip("`")
        if "/" in stripped:
            # Looks like a Discord message link — extract the trailing segment.
            link_tail = stripped.split("/")[-1]
            if link_tail.isdigit() and len(link_tail) >= 15:
                await ctx.send(brand_text(
                    "⚠️ That looks like a **Discord message link**, not the rules text.\n"
                    "`!setgangrules` (or `!setgangrules`) caches the **raw text** you paste.\n"
                    "To fetch a message's content by its ID, use:\n"
                    "`!updategangrules #channel <message_id>`"
                ))
                return
        if stripped.isdigit() and len(stripped) >= 15:
            await ctx.send(brand_text(
                "⚠️ That looks like a **message ID**, not the rules text.\n"
                "`!setgangrules` (or `!setgangrules`) caches the **raw text** you paste.\n"
                "To fetch a message's content by its ID, use:\n"
                "`!updategangrules #channel <message_id>`"
            ))
            return

        # Cache the raw text first so the verification flow always has it, even if
        # posting the public embed fails (no perms, channel missing, etc.).
        state.rules_cache['gang_rules'] = rules
        state.rules_cache['gang_last_updated'] = datetime.now(timezone.utc).isoformat()
        save_rules_cache()

        # Post (or edit) a Discohook-style embed in the gang rules channel so
        # members see a clean, Markdown-rendered rules message. The raw text goes
        # into the embed description; Discord renders bold/italic/code/quotes.
        target_channel = bot.get_channel(config.servers.gang_rules_channel)
        posted_id, status = await _post_or_edit_rules_embed(
            channel=target_channel,
            existing_message_id=config.servers.gang_rules_message,
            title=brand_text("📜 [GANG ABBR] Rules"),
            rules_text=rules,
            color=discord.Color.red(),
            footer_text=brand_text("[GANG ABBR] Rules • Last updated"),
        )
        if posted_id is not None and target_channel is not None:
            # Persist the channel + message IDs so !updategangrules (no args)
            # re-fetches this exact embed later.
            config.servers.gang_rules_channel = target_channel.id
            config.servers.gang_rules_message = posted_id
            config.save_channel_settings()

        if target_channel is None:
            status_msg = brand_text(
                "[GANG ABBR] rules **cached** for verification, but the gang rules "
                "channel isn't set, so no public embed was posted.\n"
                "Set it with `!updategangrules #channel <message_id>` or `!channelsetup`, "
                "then re-run `!setgangrules` to publish the embed."
            )
        elif posted_id is None:
            status_msg = brand_text(f"[GANG ABBR] rules **cached** for verification, but the public embed couldn't be posted:\n{status}")
        else:
            action = "edited" if status == "edited" else "posted a new"
            status_msg = brand_text(
                f"[GANG ABBR] rules **cached** and the public rules embed was **{action}** "
                f"in {target_channel.mention}.\n"
                f"**Characters:** {len(rules)}"
            )

        await ctx.send(embed=EmbedBuilder.success(brand_text("[GANG ABBR] Rules Set"), status_msg))
        logging.info(f"[RulesCache] Gang rules manually set by {ctx.author} (embed status: {status})")


    @bot.command(name="viewcachedrules", description="View the currently cached rules")
    @commands.has_permissions(administrator=True)
    async def viewcachedrules_cmd(ctx: commands.Context) -> None:
        embed = discord.Embed(title="Cached Rules Status", color=discord.Color.blurple())
    
        gang_status = f"Cached ({len(state.rules_cache['gang_rules'])} chars)" if state.rules_cache['gang_rules'] else "Not cached"
        server_status = f"Cached ({len(state.rules_cache['server_rules'])} chars)" if state.rules_cache['server_rules'] else "Not cached"
    
        embed.add_field(name=brand_text("[GANG ABBR] Rules"), value=f"{gang_status}\nLast Updated: {state.rules_cache['gang_last_updated'][:19] if state.rules_cache['gang_last_updated'] else 'Never'}", inline=True)
        embed.add_field(name="Server Rules", value=f"{server_status}\nLast Updated: {state.rules_cache['server_last_updated'][:19] if state.rules_cache['server_last_updated'] else 'Never'}", inline=True)
    
        await ctx.send(embed=embed)
