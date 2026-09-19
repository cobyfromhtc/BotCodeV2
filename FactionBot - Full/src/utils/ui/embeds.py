# -*- coding: utf-8 -*-
"""Shared embed builders — EmbedBuilder (generic branded embeds) and
VerificationEmbedBuilder (verification-flow embeds)."""

# stdlib + discord.py
import discord
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from core.state import config, data_manager
from core.helpers import brand_text, xp_for_level, xp_for_next_level



# --- EMBED BUILDER (V2 Enhancement) ---
class EmbedBuilder:
    @staticmethod
    def success(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def error(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def warning(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def info(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def verification(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def ticket(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def giveaway(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.purple(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def level(user: discord.Member, level_data: Dict) -> discord.Embed:
        embed = discord.Embed(title=f"📊 Level Card: {user.display_name}", color=discord.Color.gold())
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        
        level = level_data.get('level', 0)
        xp = level_data.get('xp', 0)
        total_messages = level_data.get('total_messages', 0)

        xp_for_next = xp_for_next_level(level)
        current_xp = xp_for_level(level)
        progress = min(100.0, ((xp - current_xp) / (xp_for_next - current_xp)) * 100) if xp_for_next > current_xp else 100.0
        
        progress_bar = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))        
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="XP", value=f"{xp:,}", inline=True)
        embed.add_field(name="Progress", value=f"`{progress_bar}` {progress:.1f}%", inline=True)
        embed.add_field(name="Messages", value=f"{total_messages:,}", inline=True)
        
        return embed

    # ------------------------------------------------------------------
    # Custom Bot Branding support (avatar / banner / footer override)
    # ------------------------------------------------------------------
    @staticmethod
    def branded(base_embed: discord.Embed, guild_id: Optional[int]) -> discord.Embed:
        """Apply per-guild custom branding (footer / color / thumbnail / image)
        to an existing embed. Falls back gracefully if no branding is configured
        or the data manager isn't ready yet (called very early in startup)."""
        try:
            if data_manager is None or data_manager._connection is None:
                return base_embed
            if guild_id is None:
                return base_embed
            branding = data_manager.get_branding(guild_id)
            footer = branding.get('embed_footer')
            if footer:
                base_embed.set_footer(text=brand_text(footer))
            color = branding.get('embed_color')
            if isinstance(color, int):
                base_embed.colour = discord.Color(color)
            thumb = branding.get('embed_thumbnail')
            if thumb:
                base_embed.set_thumbnail(url=thumb)
            img = branding.get('embed_image')
            if img:
                base_embed.set_image(url=img)
        except Exception as exc:
            logging.debug(f"[EmbedBuilder.branded] skipped branding: {exc}")
        return base_embed


# =============================================================================
# VERIFICATION EMBED BUILDER - Consistent, Modern Design
# =============================================================================

class VerificationEmbedBuilder:
    """
    Centralized embed builder for verification system.
    Ensures consistent branding and visual design across all verification messages.
    """

    COLOR_PRIMARY = 0x5865F2
    COLOR_SUCCESS = 0x57F287
    COLOR_WARNING = 0xFEE75C
    COLOR_ERROR   = 0xED4245
    COLOR_INFO    = 0x3498DB
    COLOR_GANG    = 0xFF6B35
    COLOR_GOLD    = 0xFFD700

    @staticmethod
    def create_base_embed(title: str, description: str = "", color: int = None,
                          thumbnail_url: str = None, show_brand: bool = True) -> discord.Embed:
        embed = discord.Embed(
            title=title, description=description,
            color=color or VerificationEmbedBuilder.COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc)
        )
        if show_brand:
            embed.set_footer(text=f"{config.gang_name} Verification System")
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        return embed

    @staticmethod
    def welcome_screen(user: discord.User) -> discord.Embed:
        return VerificationEmbedBuilder.create_base_embed(
            title=f"🎭 **{config.gang_name.upper()} VERIFICATION PORTAL**",
            description=(
                f"Welcome **{user.display_name}**!\n\n"
                f"You're about to begin the **{config.gang_name}** verification process.\n"
                "This will take approximately **2-3 minutes** to complete.\n\n"
                "┌─────────────────────────────┐\n"
                "│  **What to Expect:**        │\n"
                "│  • Review our server rules  │\n"
                "│  • Answer a few questions   │\n"
                "│  • Complete the server steps │\n"
                "│  • Submit for staff review  │\n"
                "└─────────────────────────────┘\n\n"
                "Press **START** when you're ready!"
            ),
            color=VerificationEmbedBuilder.COLOR_GANG,
            thumbnail_url=user.avatar.url if user.avatar else user.default_avatar.url
        )

    @staticmethod
    def rules_intro() -> discord.Embed:
        embed = VerificationEmbedBuilder.create_base_embed(
            title="📜 **RULES AGREEMENT**",
            description=(
                f"Before joining **{config.gang_name}**, you must review and agree "
                "to follow the rules of our community.\n\n"
                "**Please read both sets of rules carefully.**\n\n"
                "Click the buttons below to view each rules section.\n"
                "After reviewing, click **I AGREE** to continue."
            ),
            color=VerificationEmbedBuilder.COLOR_INFO
        )
        embed.add_field(name=f"📋 {config.gang_name} Rules",
                        value=f"Click to view {config.gang_name}'s gang rules and expectations.", inline=False)
        embed.add_field(name="📋 Server Rules",
                        value="Click to view the community server rules.", inline=False)
        return embed

    @staticmethod
    def rules_display(title: str, rules: str, server_type: str = "GNG") -> discord.Embed:
        color = VerificationEmbedBuilder.COLOR_GANG if server_type == "GANG" else VerificationEmbedBuilder.COLOR_INFO
        emoji = "🟠" if server_type == "GANG" else "🔵"
        embed = VerificationEmbedBuilder.create_base_embed(
            title=f"{emoji} **{title}**",
            description=rules[:4096] if rules else "No rules available. Contact staff.",
            color=color, show_brand=False
        )
        # When this is the gang's rules, render the configured gang abbreviation
        # in the footer (e.g. "GNG Rules" / "MOS Rules") via brand_text().
        footer_label = brand_text("[GANG ABBR] Rules") if server_type == "GANG" else f"{server_type} Rules"
        embed.set_footer(text=f"{footer_label} • Scroll to read all rules")
        return embed

    @staticmethod
    def question_screen(question_number: int, total_questions: int, question: str,
                        label: str, hint: str = None, is_agreement: bool = False) -> discord.Embed:
        progress = int((question_number / total_questions) * 100)
        progress_bar = "█" * (progress // 10) + "░" * (10 - progress // 10)
        embed = VerificationEmbedBuilder.create_base_embed(
            title=f"❓ **Question {question_number} of {total_questions}**",
            description=(
                f"```\n{progress_bar} {progress}% Complete\n```\n\n"
                f"**{label}**\n\n{question}"
            ),
            color=VerificationEmbedBuilder.COLOR_PRIMARY
        )
        if hint:
            embed.add_field(name="💡 Hint", value=hint, inline=False)
        if is_agreement:
            embed.add_field(name="⚠️ Important",
                            value="Type **'I agree'** to accept the terms and continue.", inline=False)
        return embed

    @staticmethod
    def confirmation_screen(responses: Dict[str, str]) -> discord.Embed:
        embed = VerificationEmbedBuilder.create_base_embed(
            title="📋 **REVIEW YOUR APPLICATION**",
            description=(
                "Please review your responses below before submitting.\n\n"
                "If everything looks correct, click **Submit Application**.\n"
                "If you need to make changes, click **Cancel** and restart."
            ),
            color=VerificationEmbedBuilder.COLOR_WARNING
        )
        for label, response in responses.items():
            if response:
                truncated = response[:200] + "..." if len(response) > 200 else response
                embed.add_field(name=f"• {label}", value=truncated, inline=False)
        return embed

    @staticmethod
    def submission_complete() -> discord.Embed:
        embed = VerificationEmbedBuilder.create_base_embed(
            title="🎉 **APPLICATION SUBMITTED!**",
            description=(
                "Your application has been successfully submitted for review!\n\n"
                "**What happens next?**\n"
                "• Staff will review your application\n"
                "• You'll receive a DM with the decision\n"
                "• This usually takes 1-24 hours\n\n"
                "Thank you for your patience! 🙏"
            ),
            color=VerificationEmbedBuilder.COLOR_SUCCESS
        )
        embed.add_field(name="💡 While You Wait",
                        value="Feel free to explore the server and chat with members!", inline=False)
        return embed

    @staticmethod
    def staff_submission(user: discord.User, responses: Dict[str, str], account_info: Dict) -> discord.Embed:
        embed = discord.Embed(
            title="📥 **NEW VERIFICATION SUBMISSION**",
            description=(
                f"**Applicant:** {user.mention} (`{user.name}`)\n"
                f"**User ID:** `{user.id}`"
            ),
            color=VerificationEmbedBuilder.COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
        embed.add_field(name="📊 Account Information", value=(
            f"**Account Age:** {account_info.get('account_age', 'Unknown')} days\n"
            f"**Joined Server:** {account_info.get('joined_age', 'Unknown')} days ago\n"
            f"**Created:** {account_info.get('created_at', 'Unknown')}"
        ), inline=True)
        security_flags = account_info.get('security_flags', [])
        if security_flags:
            embed.add_field(name="⚠️ Security Flags",
                            value="\n".join(f"• {flag}" for flag in security_flags), inline=True)
        else:
            embed.add_field(name="✅ Security Check", value="No security flags detected", inline=True)
        embed.add_field(name="─" * 30, value="", inline=False)
        for label, response in responses.items():
            if response:
                if label == "Invitation Proof" and response.startswith("http"):
                    embed.add_field(name=f"📸 {label}", value="📎 **[View Image]**", inline=False)
                    embed.set_image(url=response)
                else:
                    truncated = response[:300] + "..." if len(response) > 300 else response
                    embed.add_field(name=f"• {label}", value=truncated, inline=False)
        embed.set_footer(text=brand_text(f"Submitted at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} • GNG Staff Review"))
        return embed

    @staticmethod
    def decision_accepted(staff: discord.User, nickname: str = None) -> discord.Embed:
        embed = VerificationEmbedBuilder.create_base_embed(
            title="🎉 **WELCOME TO GNG!**",
            description=(
                "Congratulations! Your application has been **ACCEPTED**!\n\n"
                f"**Reviewed by:** {staff.mention}\n"
                f"{f'**Nickname:** `{nickname}`' if nickname else ''}"
            ),
            color=VerificationEmbedBuilder.COLOR_SUCCESS
        )
        embed.add_field(name="🎯 What's Next?", value=(
            "• You now have the **Member** role\n"
            "• Check out our channels and chat\n"
            "• Recruit new members for GNG!\n"
            "• Respect all members and allies\n\n"
            "**GNG × ALLIES ON TOP!** 🔥"
        ), inline=False)
        return embed

    @staticmethod
    def decision_declined(staff: discord.User, reason: str) -> discord.Embed:
        embed = VerificationEmbedBuilder.create_base_embed(
            title="❌ **APPLICATION DECLINED**",
            description=(
                "We're sorry, but your application has been declined.\n\n"
                f"**Reviewed by:** {staff.mention}\n"
                f"**Reason:** {reason}"
            ),
            color=VerificationEmbedBuilder.COLOR_ERROR
        )
        embed.add_field(name="📋 What Now?", value=(
            "• You may reapply after **24 hours**\n"
            "• Reapplying sooner may result in mod action\n"
            "• Contact staff if you have questions"
        ), inline=False)
        return embed

    @staticmethod
    def info_request(question: str, staff: discord.User) -> discord.Embed:
        embed = VerificationEmbedBuilder.create_base_embed(
            title="📝 **ADDITIONAL INFORMATION REQUESTED**",
            description=(
                f"Staff member {staff.mention} needs more information about your application.\n\n"
                f"**Question:**\n{question}"
            ),
            color=VerificationEmbedBuilder.COLOR_WARNING
        )
        embed.add_field(name="⏱️ Time Limit", value="Please respond within **5 minutes**", inline=False)
        return embed

    @staticmethod
    def timeout() -> discord.Embed:
        return VerificationEmbedBuilder.create_base_embed(
            title="⏰ **SESSION EXPIRED**",
            description=(
                "Your verification session has timed out due to inactivity.\n\n"
                "To start a new verification, type `!verify` in the verification channel."
            ),
            color=VerificationEmbedBuilder.COLOR_ERROR
        )
