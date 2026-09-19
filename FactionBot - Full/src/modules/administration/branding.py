# -*- coding: utf-8 -*-
"""Bot branding — footer/color/thumbnail/image/banner/avatar settings."""

# stdlib + discord.py
import discord
import logging
import re
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from typing import Optional

from core.state import config, data_manager
from core.helpers import brand_text
from utils.ui.embeds import EmbedBuilder




# =============================================================================
# PREMIUM FEATURES (FREE CLONES OF CARL-BOT / DYNO PREMIUM)
#   1. Reaction Roles  (Carl-bot signature — up to 250 per guild)
#   2. Sticky Roles    (Dyno premium — re-apply roles on rejoin)
#   3. Full Message Logging (Dyno premium — edit/delete content)
#   4. Custom Bot Branding  (avatar / banner / footer — premium feel)
# =============================================================================

# --- Helpers ---------------------------------------------------------------

def _hex_to_int(color_hex: str) -> Optional[int]:
    """Parse '#RRGGBB' / 'RRGGBB' / '0xRRGGBB' into an int. Returns None on bad input."""
    if not color_hex:
        return None
    raw = color_hex.strip().lstrip('#')
    if raw.lower().startswith('0x'):
        raw = raw[2:]
    if not re.fullmatch(r'[0-9a-fA-F]{6}', raw):
        return None
    return int(raw, 16)


async def _fetch_image_bytes(url: str) -> Optional[bytes]:
    """Download image bytes from a URL using aiohttp (bundled with discord.py)."""
    try:
        import aiohttp  # discord.py depends on aiohttp, so always available
    except ImportError:
        logging.error("[Branding] aiohttp not available to fetch avatar image")
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.warning(f"[Branding] avatar fetch HTTP {resp.status}")
                    return None
                data = await resp.read()
                if not data:
                    return None
                # Discord avatar limit is 10 MB; bail out early if absurdly large.
                if len(data) > 10 * 1024 * 1024:
                    logging.warning("[Branding] avatar image too large (>10MB)")
                    return None
                return data
    except Exception as exc:
        logging.warning(f"[Branding] avatar fetch failed: {exc}")
        return None

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # =========================================================================
    # 4) CUSTOM BOT BRANDING — avatar / banner / footer (premium feel)
    # =========================================================================
    @bot.group(name="botbranding", aliases=["branding"], description="Custom Bot Branding (avatar / banner / footer)")
    @app_commands.default_permissions(manage_guild=True)
    async def branding_group(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=EmbedBuilder.info(
                "Custom Bot Branding",
                "Subcommands: `name`, `footer`, `color`, `thumbnail`, `image`, `banner`, `avatar`, `view`, `clear`.",
            ), ephemeral=True)


    @branding_group.command(name="footer", description="Set a custom embed footer for this server")
    @app_commands.describe(text="Footer text (supports [GANG NAME] / [GANG ABBR] placeholders)")
    @commands.has_permissions(manage_guild=True)
    async def branding_footer(ctx: commands.Context, *, text: str) -> None:
        b = data_manager.get_branding(ctx.guild.id)
        b['embed_footer'] = text[:300]
        b['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_branding(b)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Footer Updated",
            f"Embeds will now show:\n`{brand_text(text)}`\n_(applies to new reaction-role / sticky / msglog embeds)_",
        ), ctx.guild.id))


    @branding_group.command(name="name", description="Set the community name used by [GANG NAME] branding")
    @app_commands.describe(name="The community/faction name (up to 60 chars)")
    @commands.has_permissions(manage_guild=True)
    async def branding_name(ctx: commands.Context, *, name: str) -> None:
        name = name.strip()[:60]
        if not name:
            await ctx.send(embed=EmbedBuilder.error("Invalid Name", "Please provide a non-empty name."))
            return
        old = config.gang_name
        config.gang_name = name
        try:
            config.save_branding_settings()
        except Exception as exc:
            config.gang_name = old
            await ctx.send(embed=EmbedBuilder.error("Save Failed", f"Could not persist the name: {exc}"))
            return
        await ctx.send(embed=EmbedBuilder.success(
            "Community Name Updated",
            f"Branding name set to **{name}**.\n`[GANG NAME]` placeholders now resolve to it everywhere."
        ))


    @branding_group.command(name="color", description="Set a custom embed color (hex)")
    @app_commands.describe(color="Hex color, e.g. #FF6B35 or 0xFF6B35")
    @commands.has_permissions(manage_guild=True)
    async def branding_color(ctx: commands.Context, color: str) -> None:
        color_int = _hex_to_int(color)
        if color_int is None:
            await ctx.send(embed=EmbedBuilder.error("Bad Color", "Use a 6-digit hex like `#FF6B35`."), ephemeral=True)
            return
        b = data_manager.get_branding(ctx.guild.id)
        b['embed_color'] = color_int
        b['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_branding(b)
        preview = discord.Embed(title="Color Preview", description=f"`{color}` → `#{color_int:06X}`", color=discord.Color(color_int))
        await ctx.send(embed=EmbedBuilder.branded(preview, ctx.guild.id))


    @branding_group.command(name="thumbnail", description="Set a custom embed thumbnail URL")
    @app_commands.describe(url="Direct image URL")
    @commands.has_permissions(manage_guild=True)
    async def branding_thumbnail(ctx: commands.Context, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            await ctx.send(embed=EmbedBuilder.error("Bad URL", "Thumbnail must be an http(s) URL."), ephemeral=True)
            return
        b = data_manager.get_branding(ctx.guild.id)
        b['embed_thumbnail'] = url
        b['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_branding(b)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Thumbnail Updated", "New embeds will use this thumbnail."
        ), ctx.guild.id))


    @branding_group.command(name="image", description="Set a custom embed image URL")
    @app_commands.describe(url="Direct image URL")
    @commands.has_permissions(manage_guild=True)
    async def branding_image(ctx: commands.Context, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            await ctx.send(embed=EmbedBuilder.error("Bad URL", "Image must be an http(s) URL."), ephemeral=True)
            return
        b = data_manager.get_branding(ctx.guild.id)
        b['embed_image'] = url
        b['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_branding(b)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Image Updated", "New embeds will use this image."
        ), ctx.guild.id))


    @branding_group.command(name="banner", description="Set a banner image used in branding embeds")
    @app_commands.describe(url="Direct image URL")
    @commands.has_permissions(manage_guild=True)
    async def branding_banner(ctx: commands.Context, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            await ctx.send(embed=EmbedBuilder.error("Bad URL", "Banner must be an http(s) URL."), ephemeral=True)
            return
        b = data_manager.get_branding(ctx.guild.id)
        b['banner_url'] = url
        b['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_branding(b)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Banner Updated", "Banner is shown on `!botbranding view`."
        ), ctx.guild.id))


    @branding_group.command(name="avatar", description="Change the bot's actual avatar (owner only)")
    @app_commands.describe(url="Direct image URL (png/jpg/gif)")
    @commands.is_owner()
    async def branding_avatar(ctx: commands.Context, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            await ctx.send(embed=EmbedBuilder.error("Bad URL", "Avatar must be an http(s) URL."), ephemeral=True)
            return
        await ctx.defer()
        image_bytes = await _fetch_image_bytes(url)
        if not image_bytes:
            await ctx.send(embed=EmbedBuilder.error("Download Failed", "Could not fetch the image."), ephemeral=True)
            return
        try:
            await bot.user.edit(avatar=image_bytes)
            await ctx.send(embed=EmbedBuilder.success(
                "Bot Avatar Updated ✅",
                "The bot's avatar has been changed. It may take a moment to propagate in Discord.",
            ))
        except discord.HTTPException as exc:
            if 'You are being rate limited' in str(exc) or 'rate limit' in str(exc).lower():
                await ctx.send(embed=EmbedBuilder.error("Rate Limited", "Discord limits avatar changes to ~2/hour. Try again later."), ephemeral=True)
            else:
                await ctx.send(embed=EmbedBuilder.error("Avatar Update Failed", f"Discord rejected the image: `{exc}`"), ephemeral=True)
        except Exception as exc:
            await ctx.send(embed=EmbedBuilder.error("Avatar Update Failed", f"`{exc}`"), ephemeral=True)


    @branding_group.command(name="view", description="View the current branding for this server")
    @commands.has_permissions(manage_guild=True)
    async def branding_view(ctx: commands.Context) -> None:
        b = data_manager.get_branding(ctx.guild.id)
        embed = discord.Embed(
            title=f"🎨 Branding — {ctx.guild.name}",
            description=(
                f"**Footer:** {b.get('embed_footer') or '_(not set)_'}\n"
                f"**Color:** {('#%06X' % b['embed_color']) if isinstance(b.get('embed_color'), int) else '_(not set)_'}\n"
                f"**Thumbnail:** [link]({b['embed_thumbnail']})" if b.get('embed_thumbnail') else "**Thumbnail:** _(not set)_"
            ),
            color=(discord.Color(b['embed_color']) if isinstance(b.get('embed_color'), int) else discord.Color.blurple()),
            timestamp=datetime.now(timezone.utc),
        )
        if b.get('embed_thumbnail'):
            embed.set_thumbnail(url=b['embed_thumbnail'])
        if b.get('embed_image'):
            embed.set_image(url=b['embed_image'])
        elif b.get('banner_url'):
            embed.set_image(url=b['banner_url'])
        await ctx.send(embed=embed)


    @branding_group.command(name="clear", description="Reset all branding for this server")
    @commands.has_permissions(manage_guild=True)
    async def branding_clear(ctx: commands.Context) -> None:
        data_manager.save_branding({
            'guild_id': ctx.guild.id,
            'embed_footer': None, 'embed_color': None,
            'embed_thumbnail': None, 'embed_image': None,
            'avatar_url': None, 'banner_url': None,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        await ctx.send(embed=EmbedBuilder.success("Branding Cleared", "This server will use the bot's default styling."))
