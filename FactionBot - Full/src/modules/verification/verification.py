# -*- coding: utf-8 -*-
"""Verification — V2 question flow, staff review views, verify commands."""

# stdlib + discord.py
import asyncio
import discord
import logging
import re
from datetime import datetime, timezone
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from typing import Any, Dict, List, Optional

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.helpers import _is_negative_answer, brand_text
from core.ows import ows_get
from core.process_manager import process_manager
from utils.ui.embeds import EmbedBuilder, VerificationEmbedBuilder
from modules.factions.rules import PaginatedRulesView, split_rules_into_pages



"""
# --- TEMPLATES ---
VERIFICATION_QUESTIONS: List[Tuple[str, str]] = [
    ("Before we start, do you agree to the rules and expectations of our community?", "Agreement"),
    ("What is your age? (Must be 13+ to use discord)", "Age"),
    ("What other factions are you affiliated with? (Reply 'Skip' if you're an ally)", "Gang Affiliations"),
    ("Send a screenshot of the member who invited you. (**IF YOU'RE AN ALLY MEMBER:**[Take a SS of our invite link from your faction server])", "Invitation Proof"),
    ("How good would you say your aim is, and do you use any aim trainers in your spare time? (Ex: Kovaaks, Aim Lab)", "Aim Skill & Training"),
    ("Do you have any previous experience with factions? [Ex: FiveM, Ro-Hood rp, Street shooters, Etc..]?", "Previous Gang Experience"),
    ("What is your Discord username? (NOT DISPLAY-NAME)", "Discord Username"),
    ("What do you want your faction name to be? (Format: [GANG NAME]_YourGangName)", "Desired Gang Name"),
    ("How was your experience with our verification system?", "Feedback")
]
"""

# Enhanced V2 questions used by ImprovedVerificationSystem
VERIFICATION_QUESTIONS_V2: List[Dict[str, Any]] = [
    {
        "question": "Before we start, do you agree to the Faction & VPRP Community rules and expectations?",
        "label": "Agreement",
        "hint": "Review the rules, then type 'I agree'",
        "is_agreement": True,
        "skip_allowed": False
    },
    {
        "question": "What is your age? (Must be 13+ to use discord)",
        "label": "Age",
        "hint": "Please provide your real age. This helps us understand our Community.",
        "is_agreement": False,
        "skip_allowed": False
    },
    {
        "question": "What other Factions are you affiliated with?",
        "label": "Gang Affiliations",
        "hint": "Reply 'skip' if you're an ally or not affiliated with any gangs.",
        "is_agreement": False,
        "skip_allowed": True
    },
    {
        "question": "Send a screenshot of the member who invited you.",
        "label": "Invitation Proof",
        "hint": "You can send an image here, Do NOT paste links here. (**IF YOU'RE AN ALLY MEMBER:** [Take a SS of our invite link from your faction server])",
        "is_agreement": False,
        "skip_allowed": True,
        "accepts_attachment": True
    },
    {
        "question": "How good would you say your aim is, and do you use any aim trainers in your spare time? (Ex: Kovaaks, Aim Lab)",
        "label": "Aim/Fight Skill & Training",
        "hint": "Be honest! We accept players of all skill levels.",
        "is_agreement": False,
        "skip_allowed": False
    },
    {
        "question": "Do you have any previous experience with factions? [Ex: FiveM, Ro-Hood rp, Street shooters, Etc..]?",
        "label": "Previous Faction Experience",
        "hint": "Previous experience is not required, but it helps us know your background.",
        "is_agreement": False,
        "skip_allowed": True
    },
    {
        "question": "Have you ever been part of a Leading role inside of a Gang Faction specifically? (Further questions may be asked if so)",
        "label": "Gang Leadership Experience",
        "hint": "Answer yes if you've held a leadership or organizer role in a gang faction.",
        "is_agreement": False,
        "skip_allowed": False,
        "depends_on_label": "Previous Gang Experience"
    },
    {
        "question": "What is your Roblox User? (NOT DISPLAY-NAME)",
        "label": "Roblox Username",
        "hint": "Provide your Roblox username so staff can verify your account if needed.",
        "is_agreement": False,
        "skip_allowed": False
    },
    {
        "question": "What is your Discord username? (NOT DISPLAY-NAME)",
        "label": "Discord Username",
        "hint": "Example: username#1234 or just username for new Discord format",
        "is_agreement": False,
        "skip_allowed": False
    },
    {
        "question": "What do you want your Faction name to be?",
        "label": "Desired Gang Name",
        "hint": "Format: [GANG ABBR]_YourGangName (e.g., [GANG ABBR]_Shadows)",
        "is_agreement": False,
        "skip_allowed": False
    },
    {
        "question": "How was your experience with our verification system?",
        "label": "Feedback",
        "hint": "Your feedback helps us improve! Let us know what you think.",
        "is_agreement": False,
        "skip_allowed": True
    }
]

WELCOME_TEMPLATES: List[str] = [
    "Welcome {mention} to [GANG NAME]. We expect you to put in work.",
    "What's good, {mention}? Brought any Pizza? No? Whatever. Welcome to [GANG NAME], we expect you to work hard.",
    "Yoooo big dawg {mention} just dropped in, what's good?",
    "Is that who I think it is? {mention}, welcome to [GANG NAME]. We expect you to put in work.",
    "Ay {mention}, we gotta Recruit soon, let me know when you're free Dawg.",
    "Well, well, well, if it isn't the one and only {mention}. We're glad to have you around."
]


# --- VERIFICATION VIEWS ---
class DeclineReasonModal(discord.ui.Modal, title="Decline Application"):
    reason_input: discord.ui.TextInput
    
    def __init__(self, user_id: int, user_name: str, original_embed: discord.Embed, original_view: 'VerificationButtonsView'):
        super().__init__()
        self.user_id = user_id
        self.user_name = user_name
        self.original_embed = original_embed
        self.original_view = original_view
        
        self.reason_input = discord.ui.TextInput(
            label="Reason for Decline",
            placeholder="Please provide a reason for declining this application...",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=1000,
            required=True
        )
        self.add_item(self.reason_input)
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = self.reason_input.value
        try:
            user = interaction.guild.get_member(self.user_id)
            
            self.original_embed.color = discord.Color.red()
            self.original_embed.title = f"DECLINED: {self.original_embed.title.replace('Verification Submission from ', '')}"
            self.original_embed.add_field(name="Decision", value=f"**DECLINED** by {interaction.user.mention}\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", inline=False)
            self.original_embed.add_field(name="Reason", value=reason, inline=False)
            
            for child in self.original_view.children:
                child.disabled = True
            
            await interaction.message.edit(embed=self.original_embed, view=self.original_view)
            await interaction.response.send_message(f"Application for **{self.user_name}** has been declined.", ephemeral=True)
            
            await self._notify_declined_user(user, reason)
            logging.info(f"Application declined for {self.user_name} by {interaction.user} - Reason: {reason}")
        except Exception as e:
            logging.error(f"Error in decline modal submit: {str(e)}")
            await interaction.response.send_message(f"An error occurred: {str(e)}", ephemeral=True)
    
    async def _notify_declined_user(self, user: Optional[discord.Member], reason: str) -> None:
        if not user:
            return
        try:
            decline_dm = discord.Embed(title="Application Declined", description="Your GNG application has been declined.", color=discord.Color.red())
            decline_dm.add_field(name="Reason", value=reason, inline=False)
            decline_dm.add_field(name="What Now?", value="You may reapply tomorrow, Reapplying today will likely result in Mod action.", inline=False)
            decline_dm.set_footer(text="GNG Verification Team")
            await user.send(embed=decline_dm)
        except discord.Forbidden:
            welcome_channel = state.bot.get_channel(config.channels.welcome)
            if welcome_channel:
                await welcome_channel.send(f"{user.mention} Your application has been declined. **Reason:** {reason}\nContact staff for more information.")


class RequestMoreInfoModal(discord.ui.Modal, title="Request More Information"):
    question_input: discord.ui.TextInput
    
    def __init__(self, user_id: int, user_name: str, original_embed: discord.Embed, original_view: 'VerificationButtonsView'):
        super().__init__()
        self.user_id = user_id
        self.user_name = user_name
        self.original_embed = original_embed
        self.original_view = original_view
        
        self.question_input = discord.ui.TextInput(
            label="Question for Applicant",
            placeholder="What additional information do you need from this applicant?",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=1000,
            required=True
        )
        self.add_item(self.question_input)
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        question = self.question_input.value
        try:
            user = interaction.guild.get_member(self.user_id)
            if not user:
                await interaction.response.send_message("User is no longer in the server.", ephemeral=True)
                return
            
            process_manager.add_verification(self.user_id)
            
            self.original_embed.add_field(name="Info Requested", value=f"**Question:** {question}\n**By:** {interaction.user.mention}\nWaiting for response...", inline=False)
            self.original_embed.color = discord.Color.orange()
            
            for child in self.original_view.children:
                child.disabled = True
            
            await interaction.message.edit(embed=self.original_embed, view=self.original_view)
            await interaction.response.send_message(f"Question sent to **{self.user_name}**. Waiting for their response...", ephemeral=True)
            
            response_received = await self._send_question_to_applicant(user, question, interaction)
            
            if not response_received:
                for child in self.original_view.children:
                    child.disabled = False
                self.original_embed.color = discord.Color.blurple()
                self.original_embed.add_field(name="No Response", value="The applicant did not respond in time.", inline=False)
                await interaction.message.edit(embed=self.original_embed, view=self.original_view)
            
            process_manager.remove_verification(self.user_id)
        except Exception as e:
            logging.error(f"Error in request more info modal: {str(e)}")
            process_manager.remove_verification(self.user_id)
            await interaction.response.send_message(f"An error occurred: {str(e)}", ephemeral=True)
    
    async def _send_question_to_applicant(self, user: discord.Member, question: str, staff_interaction: discord.Interaction) -> bool:
        try:
            dm_embed = discord.Embed(title="Additional Information Requested", description="The GNG staff needs more information about your application.", color=discord.Color.orange())
            dm_embed.add_field(name="Question", value=question, inline=False)
            dm_embed.add_field(name="Instructions", value="Please reply to this message with your answer within **5 minutes**.", inline=False)
            dm_embed.set_footer(text="GNG Verification Team")
            await user.send(embed=dm_embed)
            
            try:
                response_msg = await state.bot.wait_for('message', check=lambda m: m.author.id == self.user_id and m.channel.type == discord.ChannelType.private, timeout=300)
                
                response_text = response_msg.content
                if response_msg.attachments:
                    response_text += f"\n\nAttachment: {response_msg.attachments[0].url}"
                
                self.original_embed.add_field(name=f"Applicant Response", value=f"**Answer:** {response_text[:1000]}", inline=False)
                self.original_embed.color = discord.Color.blurple()
                
                for i, field in enumerate(self.original_embed.fields):
                    if field.name == "Info Requested":
                        self.original_embed.set_field_at(i, name="Info Requested", value=f"**Question:** {question}\n**By:** {staff_interaction.user.mention}\nResponse received!", inline=False)
                        break
                
                for child in self.original_view.children:
                    child.disabled = False
                
                channel = state.bot.get_channel(config.channels.verification_submission)
                if channel:
                    try:
                        message = await channel.fetch_message(staff_interaction.message.id)
                        await message.edit(embed=self.original_embed, view=self.original_view)
                        await channel.send(f"**{user.display_name}** has responded to the info request!", delete_after=30)
                    except discord.NotFound:
                        pass
                
                confirm_embed = discord.Embed(title="Response Received", description="Thank you! Your response has been sent to the staff for review.", color=discord.Color.green())
                await user.send(embed=confirm_embed)
                return True
                
            except asyncio.TimeoutError:
                timeout_embed = discord.Embed(title="Time Expired", description="You did not respond in time. Please contact staff directly.", color=discord.Color.red())
                try:
                    await user.send(embed=timeout_embed)
                except discord.Forbidden:
                    pass
                return False
                
        except discord.Forbidden:
            await staff_interaction.followup.send(f"Could not DM {user.mention}. They may have DMs disabled.", ephemeral=True)
            return False
        except Exception as e:
            logging.error(f"Error sending question to applicant: {str(e)}")
            return False


class VerificationButtonsView(View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="Accept Application", style=discord.ButtonStyle.success, custom_id="accept_button")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            embed = interaction.message.embeds[0]
            footer_text = embed.footer.text
            
            if "User ID:" not in footer_text:
                await interaction.response.send_message(
                    embed=EmbedBuilder.error("Parse Error", "Could not find the user ID in the application footer."),
                    ephemeral=True,
                )
                return
            
            user_id = int(footer_text.split("User ID: ")[1].split(" ")[0])
            user = interaction.guild.get_member(user_id)
            
            if not user:
                await interaction.response.send_message(
                    embed=EmbedBuilder.warning("User Not Found", "That user is no longer in the server."),
                    ephemeral=True,
                )
                return
            
            member_role = interaction.guild.get_role(config.roles.member)
            
            if member_role:
                await user.add_roles(member_role)
                embed.color = discord.Color.green()
                embed.title = f"ACCEPTED: {embed.title.replace('Verification Submission from ', '')}"
                embed.add_field(name="Decision", value=f"**ACCEPTED** by {interaction.user.mention}\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", inline=False)
                
                for child in self.children:
                    child.disabled = True
                
                await interaction.message.edit(embed=embed, view=self)

                # --- SET NICKNAME: GNG_GangName ---
                nickname_set = False
                try:
                    gang_name = None

                    for field in embed.fields:
                        if field.name == "Desired Gang Name":
                            gang_name = field.value.strip()

                    if gang_name:
                        new_nickname = gang_name[:32]
                        await user.edit(nick=new_nickname)
                        nickname_set = True
                        logging.info(f"Nickname set to '{new_nickname}' for {user}")
                    else:
                        logging.warning(f"Could not set nickname for {user}: gang_name={gang_name}")
                except discord.Forbidden:
                    logging.warning(f"Missing permissions to change nickname for {user}")
                except Exception as nick_err:
                    logging.error(f"Failed to set nickname for {user}: {nick_err}")
                # ----------------------------------------------------------

                nick_note = f"\nNickname set to **{new_nickname}**" if nickname_set else "\n⚠️ Could not auto-set nickname (missing permissions or data)."
                await interaction.response.send_message(
                    embed=EmbedBuilder.success(
                        "Application Accepted",
                        f"{user.mention}'s application has been accepted. Member role assigned.{nick_note}",
                    ),
                    ephemeral=True
                )
                
                await self._notify_accepted_user(user)
                logging.info(f"Application accepted for {user} by {interaction.user}")
            else:
                await interaction.response.send_message(
                    embed=EmbedBuilder.error("Role Missing", "Could not find the member role to assign. Check the bot configuration."),
                    ephemeral=True,
                )
        except Exception as e:
            logging.error(f"Error in accept button handler: {str(e)}")
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Unexpected Error", f"An error occurred while accepting the application: `{e}`"),
                ephemeral=True,
            )
    
    @discord.ui.button(label="Decline Application", style=discord.ButtonStyle.danger, custom_id="decline_button")
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            embed = interaction.message.embeds[0]
            footer_text = embed.footer.text
            
            if "User ID:" not in footer_text:
                await interaction.response.send_message(
                    embed=EmbedBuilder.error("Parse Error", "Could not find the user ID in the application footer."),
                    ephemeral=True,
                )
                return
            
            user_id = int(footer_text.split("User ID: ")[1].split(" ")[0])
            user_name = embed.title.replace("Verification Submission from ", "")
            
            modal = DeclineReasonModal(user_id, user_name, embed, self)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logging.error(f"Error in decline button handler: {str(e)}")
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Unexpected Error", f"An error occurred while declining the application: `{e}`"),
                ephemeral=True,
            )
    
    @discord.ui.button(label="Request More Info", style=discord.ButtonStyle.secondary, custom_id="request_info_button")
    async def request_info_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            embed = interaction.message.embeds[0]
            footer_text = embed.footer.text
            
            if "User ID:" not in footer_text:
                await interaction.response.send_message(
                    embed=EmbedBuilder.error("Parse Error", "Could not find the user ID in the application footer."),
                    ephemeral=True,
                )
                return
            
            user_id = int(footer_text.split("User ID: ")[1].split(" ")[0])
            user_name = embed.title.replace("Verification Submission from ", "")
            
            user = interaction.guild.get_member(user_id)
            if not user:
                await interaction.response.send_message(
                    embed=EmbedBuilder.warning("User Not Found", "This user is no longer in the server."),
                    ephemeral=True,
                )
                return
            
            modal = RequestMoreInfoModal(user_id, user_name, embed, self)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logging.error(f"Error in request info button handler: {str(e)}")
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Unexpected Error", f"An error occurred while requesting info: `{e}`"),
                ephemeral=True,
            )
    
    async def _notify_accepted_user(self, user: discord.Member) -> None:
        try:
            accept_dm = discord.Embed(
                title="🎉 Application Accepted!",
                description="Welcome to GNG! Your application has been accepted.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            accept_dm.add_field(name="What Now?", value="You now have the Member role. You're officially part of the GNG Blood.. Go on and Recruit new members, Chill with GNG Members and Allies, And just enjoy life man. Because that's what it's all about. GNG x ALLIES ON TOP!", inline=False)
            accept_dm.set_footer(text=f"Accepted by GNG Staff")
            await user.send(embed=accept_dm)
        except discord.Forbidden:
            welcome_channel = state.bot.get_channel(config.channels.welcome)
            if welcome_channel:
                await welcome_channel.send(f"{user.mention} Your application has been accepted! Welcome to GNG!")


# =============================================================================
# IMPROVED VERIFICATION VIEWS - Button-based interaction components
# =============================================================================

class WelcomeView(View):
    """Initial welcome screen with Start and Cancel buttons."""

    def __init__(self, user_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.started = False

    @discord.ui.button(label="🚀 START VERIFICATION", style=discord.ButtonStyle.success,
                       custom_id="v2_start_verification")
    async def start_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        self.started = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary,
                       custom_id="v2_cancel_verification")
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        self.started = False
        self.stop()
        for child in self.children:
            child.disabled = True
        embed = VerificationEmbedBuilder.create_base_embed(
            title="❌ **VERIFICATION CANCELLED**",
            description="You've cancelled the verification process. Type `!verify` to start again.",
            color=VerificationEmbedBuilder.COLOR_ERROR
        )
        await interaction.response.edit_message(embed=embed, view=self)


class ImprovedRulesView(View):
    """Rules review with GNG/Server buttons and agreement confirmation."""

    def __init__(self, user_id: int, gang_rules: str, server_rules: str, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.gang_rules = gang_rules
        self.server_rules = server_rules
        self.viewed = {'gang': False, 'server': False}
        self.agreed = False
        # Apply branded button labels at instance time. config.load_branding_settings()
        # runs in setup_hook (before any view is created), so brand_text() resolves
        # [GANG ABBR] to the configured abbreviation rather than baking the default
        # at class-definition time.
        self.gang_button.label = f"🟠 {brand_text('[GANG ABBR] Rules')}"

    def _update_buttons(self):
        if self.viewed['gang']:
            self.gang_button.style = discord.ButtonStyle.success
            self.gang_button.label = f"✅ {brand_text('[GANG ABBR] Rules Viewed')}"
        if self.viewed['server']:
            self.server_button.style = discord.ButtonStyle.success
            self.server_button.label = "✅ Server Rules Viewed"
        if self.viewed['gang'] and self.viewed['server']:
            self.agree_button.disabled = False
            self.agree_button.style = discord.ButtonStyle.success

    @discord.ui.button(label="🟠 [GANG ABBR] Rules", style=discord.ButtonStyle.primary,
                       custom_id="v2_view_gang_rules", emoji="📜")
    async def gang_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        pages = split_rules_into_pages(self.gang_rules)
        if len(pages) > 1:
            view = PaginatedRulesView(self.user_id, brand_text("[GANG ABBR] In-Game Rules"), "", discord.Color.red(), pages)
            await interaction.response.send_message(embed=view._create_embed(), view=view, ephemeral=True)
        else:
            embed = VerificationEmbedBuilder.rules_display(brand_text("[GANG ABBR] In-Game Rules"), self.gang_rules, "GNG")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        self.viewed['gang'] = True
        self._update_buttons()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="🔵 Server Rules", style=discord.ButtonStyle.primary,
                       custom_id="v2_view_server_rules", emoji="📜")
    async def server_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        pages = split_rules_into_pages(self.server_rules)
        if len(pages) > 1:
            view = PaginatedRulesView(self.user_id, "Server Rules", "", discord.Color.blue(), pages)
            await interaction.response.send_message(embed=view._create_embed(), view=view, ephemeral=True)
        else:
            embed = VerificationEmbedBuilder.rules_display("Server Rules", self.server_rules, "Server")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        self.viewed['server'] = True
        self._update_buttons()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✓ I AGREE TO ALL RULES", style=discord.ButtonStyle.secondary,
                       custom_id="v2_agree_rules", disabled=True, row=1)
    async def agree_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        if not (self.viewed['gang'] and self.viewed['server']):
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Rules Not Reviewed", "Please view BOTH rules sections before agreeing."),
                ephemeral=True,
            )
            return
        self.agreed = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger,
                       custom_id="v2_cancel_rules", row=1)
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        self.agreed = False
        self.stop()




class ImprovedConfirmationView(View):
    """Final confirmation view with Submit and Cancel buttons."""

    def __init__(self, user_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.confirmed = False

    @discord.ui.button(label="✅ SUBMIT APPLICATION", style=discord.ButtonStyle.success,
                       custom_id="v2_confirm_submit")
    async def submit_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        self.confirmed = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=VerificationEmbedBuilder.create_base_embed(
                title="⏳ **SUBMITTING...**",
                description="Please wait while we process your application...",
                color=VerificationEmbedBuilder.COLOR_INFO
            ),
            view=self
        )

    @discord.ui.button(label="❌ CANCEL", style=discord.ButtonStyle.danger,
                       custom_id="v2_confirm_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Session", "This verification session belongs to someone else. Start your own with `!verify`."),
                ephemeral=True,
            )
            return
        self.confirmed = False
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=VerificationEmbedBuilder.create_base_embed(
                title="❌ **CANCELLED**",
                description="Your application has been discarded. Type `!verify` to start again.",
                color=VerificationEmbedBuilder.COLOR_ERROR
            ),
            view=self
        )


# =============================================================================
# IMPROVED STAFF ACTION VIEW — replaces old VerificationButtonsView for new
# submissions. Old VerificationButtonsView is kept for backward compatibility.
# =============================================================================

class ImprovedStaffActionView(View):
    """Full-featured staff action view with Accept/Decline/Request Info/Note."""

    def __init__(self, user_id: int, user_name: str, submission_embed: discord.Embed):
        super().__init__(timeout=None)  # Persistent
        self.target_user_id = user_id
        self.user_name = user_name
        self.submission_embed = submission_embed

    def disable_all(self):
        for child in self.children:
            child.disabled = True

    async def _get_state_from_embed(self, interaction: discord.Interaction):
        """
        Extract user_id and name from the submission embed so staff actions
        survive bot restarts (persistent views are re-registered with dummy
        state on startup, so the real state must be recovered from the
        message embed itself).

        Robust to embed format drift: tries multiple parse strategies and
        logs a warning instead of failing silently when parsing fails.
        """
        embed = interaction.message.embeds[0] if interaction.message.embeds else self.submission_embed
        desc = embed.description or ""
        fields = {f.name: (f.value or "") for f in embed.fields}

        user_id = self.target_user_id
        user_name = self.user_name

        # ── User ID ───────────────────────────────────────────────────
        parsed_uid = None
        # Strategy 1: the canonical "**User ID:** `123`" token.
        if "**User ID:** `" in desc:
            try:
                parsed_uid = int(desc.split("**User ID:** `")[1].split("`")[0])
            except (ValueError, IndexError) as exc:
                logging.warning(f"[StaffActionView] Failed to parse user_id via primary pattern: {exc}")
        # Strategy 2: any "User ID" field/label followed by a snowflake.
        if parsed_uid is None:
            haystack = desc + "\n" + "\n".join(f"{k}: {v}" for k, v in fields.items())
            m = re.search(r"User\s*ID:?\s*`?(\d{15,20})`?", haystack, re.IGNORECASE)
            if m:
                parsed_uid = int(m.group(1))
        # Strategy 3: bare snowflake in a dedicated field named "User ID".
        if parsed_uid is None and "User ID" in fields:
            m = re.search(r"(\d{15,20})", fields["User ID"])
            if m:
                parsed_uid = int(m.group(1))
        if parsed_uid is not None:
            user_id = parsed_uid
        elif self.target_user_id == 0:
            logging.error(
                "[StaffActionView] Could not recover target user_id from embed "
                f"(message_id={getattr(interaction.message, 'id', '?')}). "
                "Staff action will be blocked to prevent operating on the wrong user."
            )

        # ── User name ─────────────────────────────────────────────────
        parsed_name = None
        if "**Applicant:**" in desc:
            try:
                # Canonical: **Applicant:** {mention} (`name`)
                parsed_name = desc.split("**Applicant:** ")[1].split("`")[1]
            except IndexError:
                # Fallback: strip Markdown/mentions from the rest of the line.
                try:
                    rest = desc.split("**Applicant:** ")[1].split("\n")[0]
                    cleaned = re.sub(r"<@!?\d+>", "", rest).replace("`", "").strip(" -*•")
                    parsed_name = cleaned or None
                except Exception:
                    parsed_name = None
        if parsed_name:
            user_name = parsed_name
        elif not self.user_name and parsed_name is None:
            logging.warning("[StaffActionView] Could not recover applicant name from embed.")

        return user_id, user_name, embed

    def _state_unrecoverable(self, user_id: int, interaction: discord.Interaction) -> bool:
        """Returns True if we could not recover the applicant's user_id."""
        if user_id and user_id != 0:
            return False
        # Block the action: opening a modal against user_id=0 would silently
        # accept/decline the wrong (non-existent) applicant.
        logging.error(
            f"[StaffActionView] Blocking staff action on message "
            f"{getattr(interaction.message, 'id', '?')} - applicant user_id "
            "could not be recovered from the embed."
        )
        return True

    @discord.ui.button(label="✅ ACCEPT", style=discord.ButtonStyle.success,
                       custom_id="improved_staff_accept", emoji="👍")
    async def accept_button(self, interaction: discord.Interaction, button: Button):
        user_id, user_name, embed = await self._get_state_from_embed(interaction)
        if self._state_unrecoverable(user_id, interaction):
            await interaction.response.send_message(
                embed=EmbedBuilder.error(
                    "Applicant Unidentifiable",
                    "This submission's applicant could not be identified (the embed format may have changed). Please process it manually.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ImprovedAcceptModal(user_id, user_name, embed, self)
        )

    @discord.ui.button(label="❌ DECLINE", style=discord.ButtonStyle.danger,
                       custom_id="improved_staff_decline", emoji="👎")
    async def decline_button(self, interaction: discord.Interaction, button: Button):
        user_id, user_name, embed = await self._get_state_from_embed(interaction)
        if self._state_unrecoverable(user_id, interaction):
            await interaction.response.send_message(
                embed=EmbedBuilder.error(
                    "Applicant Unidentifiable",
                    "This submission's applicant could not be identified (the embed format may have changed). Please process it manually.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ImprovedDeclineModal(user_id, user_name, embed, self)
        )

    @discord.ui.button(label="❓ REQUEST INFO", style=discord.ButtonStyle.secondary,
                       custom_id="improved_staff_request_info", emoji="💬")
    async def request_info_button(self, interaction: discord.Interaction, button: Button):
        user_id, user_name, embed = await self._get_state_from_embed(interaction)
        if self._state_unrecoverable(user_id, interaction):
            await interaction.response.send_message(
                embed=EmbedBuilder.error(
                    "Applicant Unidentifiable",
                    "This submission's applicant could not be identified (the embed format may have changed). Please process it manually.",
                ),
                ephemeral=True,
            )
            return
        # Capture the staff submission message + channel IDs here (button click
        # interactions reliably populate interaction.message/channel) so the
        # modal's on_submit and the detached reply-listener can later edit the
        # SAME staff message to append the applicant's answer.
        staff_message_id = interaction.message.id if interaction.message else None
        staff_channel_id = interaction.channel.id if interaction.channel else None
        await interaction.response.send_modal(
            ImprovedRequestInfoModal(user_id, user_name, embed, self, staff_message_id, staff_channel_id)
        )

    @discord.ui.button(label="📝 ADD NOTE", style=discord.ButtonStyle.primary,
                       custom_id="improved_staff_note", emoji="📌", row=1)
    async def note_button(self, interaction: discord.Interaction, button: Button):
        user_id, user_name, embed = await self._get_state_from_embed(interaction)
        if self._state_unrecoverable(user_id, interaction):
            await interaction.response.send_message(
                embed=EmbedBuilder.error(
                    "Applicant Unidentifiable",
                    "This submission's applicant could not be identified (the embed format may have changed). Please process it manually.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ImprovedStaffNoteModal(user_id, embed, self)
        )


class ImprovedAcceptModal(Modal, title="✅ Accept Application"):
    gang_name = TextInput(
        label="Gang Name (optional — sets nickname)",
        placeholder="GNG_GangName",
        required=False,
        max_length=32
    )
    notes = TextInput(
        label="Staff Notes (internal)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    def __init__(self, user_id: int, user_name: str, embed: discord.Embed, view: ImprovedStaffActionView):
        super().__init__()
        self.target_user_id = user_id
        self.user_name = user_name
        self.embed = embed
        self.staff_view = view

    async def on_submit(self, interaction: discord.Interaction):
        # ── 1. Update the submission embed ──────────────────────────────────
        self.embed.color = VerificationEmbedBuilder.COLOR_SUCCESS
        self.embed.title = f"✅ ACCEPTED: {self.user_name}"
        self.embed.add_field(
            name="════════ DECISION ════════",
            value=(
                f"**Status:** ✅ ACCEPTED\n"
                f"**By:** {interaction.user.mention}\n"
                f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            inline=False
        )
        if self.gang_name.value:
            self.embed.add_field(name="Assigned Gang Name", value=f"`{self.gang_name.value}`", inline=False)
        if self.notes.value:
            self.embed.add_field(name="📝 Staff Notes", value=self.notes.value, inline=False)

        self.staff_view.disable_all()
        await interaction.response.edit_message(embed=self.embed, view=self.staff_view)

        # ── 2. Assign member role ────────────────────────────────────────────
        user = interaction.guild.get_member(self.target_user_id)
        nickname_text = ""
        if user:
            member_role = interaction.guild.get_role(config.roles.member)
            if member_role:
                try:
                    await user.add_roles(member_role)
                except Exception as e:
                    logging.warning(f"[ImprovedAccept] Could not add member role: {e}")

            # ── 3. Set nickname to provided gang name only ────────────────────
            try:
                gang = self.gang_name.value.strip() if self.gang_name.value else None
                new_nick = gang[:32] if gang else None
                if new_nick:
                    await user.edit(nick=new_nick)
                    nickname_text = f"\nNickname set to **{new_nick}**"
                    logging.info(f"[ImprovedAccept] Nickname '{new_nick}' set for {user}")
            except discord.Forbidden:
                nickname_text = "\n⚠️ Could not set nickname (missing permissions)."
                logging.warning(f"[ImprovedAccept] Missing perms to set nickname for {user}")
            except Exception as e:
                nickname_text = "\n⚠️ Nickname not set (error)."
                logging.error(f"[ImprovedAccept] Nickname error: {e}")

            # ── 4. DM the applicant ──────────────────────────────────────────
            try:
                accept_embed = VerificationEmbedBuilder.decision_accepted(
                    interaction.user, self.gang_name.value or None
                )
                await user.send(embed=accept_embed)
            except discord.Forbidden:
                welcome_channel = state.bot.get_channel(config.channels.welcome)
                if welcome_channel:
                    await welcome_channel.send(
                        brand_text(f"{user.mention} Your application has been accepted! Welcome to GNG!"))

        await interaction.followup.send(
            embed=EmbedBuilder.success(
                "Application Accepted",
                f"**{self.user_name}**'s application has been accepted!{nickname_text}",
            ),
            ephemeral=True
        )
        logging.info(f"[ImprovedAccept] {self.user_name} accepted by {interaction.user}")


class ImprovedDeclineModal(Modal, title="❌ Decline Application"):
    reason = TextInput(
        label="Reason for decline",
        style=discord.TextStyle.paragraph,
        placeholder="Please provide a clear reason...",
        required=True,
        min_length=10,
        max_length=1000
    )

    def __init__(self, user_id: int, user_name: str, embed: discord.Embed, view: ImprovedStaffActionView):
        super().__init__()
        self.target_user_id = user_id
        self.user_name = user_name
        self.embed = embed
        self.staff_view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.embed.color = VerificationEmbedBuilder.COLOR_ERROR
        self.embed.title = f"❌ DECLINED: {self.user_name}"
        self.embed.add_field(
            name="════════ DECISION ════════",
            value=(
                f"**Status:** ❌ DECLINED\n"
                f"**By:** {interaction.user.mention}\n"
                f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            inline=False
        )
        self.embed.add_field(name="Reason", value=self.reason.value, inline=False)
        self.staff_view.disable_all()
        await interaction.response.edit_message(embed=self.embed, view=self.staff_view)

        # DM the applicant
        user = interaction.guild.get_member(self.target_user_id)
        if user:
            try:
                decline_embed = VerificationEmbedBuilder.decision_declined(interaction.user, self.reason.value)
                await user.send(embed=decline_embed)
            except discord.Forbidden:
                welcome_channel = state.bot.get_channel(config.channels.welcome)
                if welcome_channel:
                    await welcome_channel.send(
                    brand_text(f"{user.mention} Your application has been declined. **Reason:** {self.reason.value}"))

        await interaction.followup.send(
            embed=EmbedBuilder.error(
                "Application Declined",
                f"**{self.user_name}**'s application has been declined.",
            ),
            ephemeral=True
        )
        logging.info(f"[ImprovedDecline] {self.user_name} declined by {interaction.user}: {self.reason.value}")


class ImprovedRequestInfoModal(Modal, title="❓ Request Additional Info"):
    question = TextInput(
        label="Question for applicant",
        style=discord.TextStyle.paragraph,
        placeholder="What additional information do you need?",
        required=True,
        min_length=10,
        max_length=1000
    )

    def __init__(self, user_id: int, user_name: str, embed: discord.Embed,
                 view: ImprovedStaffActionView, staff_message_id: Optional[int] = None,
                 staff_channel_id: Optional[int] = None):
        super().__init__()
        self.target_user_id = user_id
        self.user_name = user_name
        self.embed = embed
        self.staff_view = view
        # ID of the staff submission message (in verification_submission) that
        # the applicant's eventual reply must be appended to. Captured at
        # button-click time because modal on_submit may not reliably expose
        # interaction.message.
        self.staff_message_id = staff_message_id
        self.staff_channel_id = staff_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        self.embed.color = VerificationEmbedBuilder.COLOR_WARNING
        self.embed.add_field(
            name="════════ INFO REQUESTED ════════",
            value=(
                f"**Question:** {self.question.value}\n"
                f"**By:** {interaction.user.mention}\n"
                "⏳ Waiting for response..."
            ),
            inline=False
        )
        await interaction.response.edit_message(embed=self.embed)

        # Fallback: if the button handler somehow didn't capture IDs, recover
        # them from the interaction now (best-effort).
        if self.staff_message_id is None and interaction.message:
            self.staff_message_id = interaction.message.id
        if self.staff_channel_id is None and interaction.channel:
            self.staff_channel_id = interaction.channel.id

        staff_user = interaction.user
        question_text = self.question.value

        # DM the applicant the question
        user = interaction.guild.get_member(self.target_user_id)
        if user:
            try:
                info_embed = VerificationEmbedBuilder.info_request(question_text, staff_user)
                await user.send(embed=info_embed)
            except discord.Forbidden:
                pass

        await interaction.followup.send(
            embed=EmbedBuilder.info(
                "Question Sent",
                f"❓ Your question has been sent to **{self.user_name}**. Waiting for their response (up to 5 minutes)...",
            ),
            ephemeral=True
        )

        # Spawn the DETACHED listener that waits for the applicant's DM reply
        # and forwards it back into the Application Viewing channel. on_submit
        # must return promptly; the up-to-5-minute wait happens in the
        # background so the modal interaction does not time out.
        asyncio.create_task(
            self._await_applicant_response(user, question_text, staff_user)
        )

    async def _await_applicant_response(
        self,
        user: Optional[discord.Member],
        question: str,
        staff_user: discord.User,
    ) -> None:
        """Wait for the applicant's DM reply and forward it to the Application Viewing channel."""
        if user is None:
            logging.warning(
                f"[RequestInfo] Applicant {self.target_user_id} left the guild before answering."
            )
            await self._edit_staff_embed(
                question, staff_user,
                "❌ Applicant left the server before responding.", received=False
            )
            return

        process_manager.add_verification(self.target_user_id)
        try:
            try:
                response_msg = await state.bot.wait_for(
                    'message',
                    check=lambda m: (
                        m.author.id == self.target_user_id
                        and m.channel.type == discord.ChannelType.private
                    ),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                # Applicant didn't reply within 5 minutes. Reflect this on the
                # staff submission embed and DM the applicant a timeout notice.
                await self._edit_staff_embed(
                    question, staff_user,
                    "❌ No response received within 5 minutes.", received=False
                )
                try:
                    timeout_dm = EmbedBuilder.warning(
                        "⏰ Time Expired",
                        "You did not respond in time. Please contact staff directly to continue your verification.",
                    )
                    timeout_dm.set_footer(text="Verification Info Request")
                    await user.send(embed=timeout_dm)
                except discord.Forbidden:
                    pass
                return

            # Build the response text (text + first attachment URL if present)
            response_text = response_msg.content or "(no text)"
            if response_msg.attachments:
                response_text += f"\n\nAttachment: {response_msg.attachments[0].url}"

            await self._edit_staff_embed(question, staff_user, response_text, received=True)

            # Confirm receipt to the applicant
            confirm_embed = EmbedBuilder.success(
                "✅ Response Received",
                "Thank you! Your response has been sent to the staff for review. They'll get back to you shortly.",
            )
            confirm_embed.set_footer(text="Verification Info Request")
            try:
                await user.send(embed=confirm_embed)
            except discord.Forbidden:
                pass
        except Exception as e:
            logging.error(f"[RequestInfo] Error awaiting applicant response: {e}")
        finally:
            process_manager.remove_verification(self.target_user_id)

    async def _edit_staff_embed(
        self,
        question: str,
        staff_user: discord.User,
        response_text: str,
        received: bool,
    ) -> None:
        """Edit the original staff submission embed to append the applicant's reply."""
        if self.staff_channel_id is None or self.staff_message_id is None:
            logging.warning("[RequestInfo] Cannot edit staff embed — message/channel ID missing.")
            return
        channel = state.bot.get_channel(self.staff_channel_id)
        if channel is None:
            logging.warning(f"[RequestInfo] Staff channel {self.staff_channel_id} not found.")
            return
        try:
            message = await channel.fetch_message(self.staff_message_id)
        except discord.NotFound:
            logging.warning(f"[RequestInfo] Staff submission message {self.staff_message_id} not found.")
            return
        except discord.Forbidden:
            logging.warning("[RequestInfo] No permission to fetch the staff submission message.")
            return
        except Exception as e:
            logging.error(f"[RequestInfo] Unexpected error fetching staff message: {e}")
            return

        embed = message.embeds[0] if message.embeds else self.embed
        # Update the existing "INFO REQUESTED" field so it reflects the outcome.
        updated_info = (
            f"**Question:** {question}\n"
            f"**By:** {staff_user.mention}\n"
            f"{'✅ Response received!' if received else '❌ No response received.'}"
        )
        replaced = False
        for i, field in enumerate(embed.fields):
            if "INFO REQUESTED" in field.name:
                embed.set_field_at(i, name=field.name, value=updated_info, inline=False)
                replaced = True
                break
        if not replaced:
            embed.add_field(
                name="════════ INFO REQUESTED ════════",
                value=updated_info, inline=False
            )
        # Append the applicant's answer (or timeout notice) as a new field.
        embed.add_field(
            name="📝 Applicant Response",
            value=f"**Answer:** {response_text[:1000]}",
            inline=False
        )
        embed.color = discord.Color.blurple() if received else discord.Color.red()
        try:
            await message.edit(embed=embed, view=self.staff_view)
            notice = (
                f"**{self.user_name}** has responded to the info request!"
                if received else
                f"**{self.user_name}** did not respond to the info request within 5 minutes."
            )
            await channel.send(notice, delete_after=30)
        except Exception as e:
            logging.error(f"[RequestInfo] Error editing staff submission message: {e}")


class ImprovedStaffNoteModal(Modal, title="📝 Add Staff Note"):
    note = TextInput(
        label="Internal note",
        style=discord.TextStyle.paragraph,
        placeholder="This note is only visible to staff...",
        required=True,
        max_length=1000
    )

    def __init__(self, user_id: int, embed: discord.Embed, view: ImprovedStaffActionView):
        super().__init__()
        self.target_user_id = user_id
        self.embed = embed
        self.staff_view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.embed.add_field(
            name=f"📝 Staff Note by {interaction.user.display_name}",
            value=self.note.value,
            inline=False
        )
        await interaction.response.edit_message(embed=self.embed)
        await interaction.followup.send(
            embed=EmbedBuilder.success("Note Added", "Your staff note has been attached to the submission."),
            ephemeral=True
        )


# =============================================================================
# IMPROVED VERIFICATION SYSTEM - Full flow using new UI components
# =============================================================================

class ImprovedVerificationSystem:
    """Drop-in improved verification flow using the new UI components."""

    def __init__(self, bot_instance, cfg, data_mgr):
        self.bot = bot_instance
        self.config = cfg
        self.data_manager = data_mgr

    async def start_verification(self, ctx) -> bool:
        user = ctx.author
        now = datetime.now(timezone.utc)
        created_at = user.created_at.replace(tzinfo=timezone.utc) if user.created_at.tzinfo is None else user.created_at

        if ows_get("enforce_account_age"):
            if (now - created_at).days < self.config.limits.min_account_age_days:
                await ctx.send(embed=VerificationEmbedBuilder.create_base_embed(
                    title="❌ **ACCOUNT TOO NEW**",
                    description=(
                        f"Your Discord account is too new to verify.\n\n"
                        f"**Account Age:** {(now - created_at).days} days\n"
                        f"**Required:** {self.config.limits.min_account_age_days} days\n\n"
                        "Please wait and try again later."
                    ),
                    color=VerificationEmbedBuilder.COLOR_ERROR
                ))
                return False

        member_role = ctx.guild.get_role(self.config.roles.member)
        tester_role = ctx.guild.get_role(self.config.roles.server_tester)
        is_tester = tester_role and tester_role in user.roles
        if member_role and member_role in user.roles and not is_tester:
            await ctx.send(embed=VerificationEmbedBuilder.create_base_embed(
                title="✅ **ALREADY VERIFIED**",
                description="You are already a verified member of GNG!",
                color=VerificationEmbedBuilder.COLOR_SUCCESS
            ))
            return False

        try:
            welcome_embed = VerificationEmbedBuilder.welcome_screen(user)
            welcome_view = WelcomeView(user.id, timeout=300)
            await user.send(embed=welcome_embed, view=welcome_view)
            mainchannel = self.bot.get_channel(self.config.channels.verification_main)
            if mainchannel:
                await mainchannel.send(f"📬 Check your DMs, {user.mention}!")

            await welcome_view.wait()
            if not welcome_view.started:
                return False

            process_manager.add_verification(user.id)

            responses = await self._collect_responses(user)
            if responses is None:
                process_manager.remove_verification(user.id)
                return False

            confirmed = await self._show_confirmation(user, responses)
            if not confirmed:
                process_manager.remove_verification(user.id)
                return False

            await self._submit_to_staff(ctx, user, responses)
            process_manager.remove_verification(user.id)
            return True

        except discord.Forbidden:
            mainchannel = self.bot.get_channel(self.config.channels.verification_main)
            if mainchannel:
                await mainchannel.send(
                    f"❌ {user.mention}, I couldn't send you a DM! "
                    "Please enable Direct Messages from server members and try again."
                )
            return False
        except Exception as e:
            process_manager.remove_verification(user.id)
            logging.error(f"[ImprovedVerification] Error for {user}: {e}")
            try:
                await user.send("An error occurred during verification. Please contact staff.")
            except discord.Forbidden:
                pass
            return False

    async def _collect_responses(self, user) -> Optional[Dict[str, str]]:
        responses: Dict[str, str] = {}
        total = len(VERIFICATION_QUESTIONS_V2)

        for idx, q_data in enumerate(VERIFICATION_QUESTIONS_V2, 1):
            label          = q_data["label"]
            question       = q_data["question"]
            hint           = q_data.get("hint")
            is_agreement   = q_data.get("is_agreement", False)
            skip_allowed   = q_data.get("skip_allowed", False)
            accepts_attach = q_data.get("accepts_attachment", False)

            # ── Rules agreement step ─────────────────────────────────────
            if is_agreement:
                agreed = await self._handle_agreement(user)
                if not agreed:
                    await user.send(embed=VerificationEmbedBuilder.create_base_embed(
                        title="❌ **VERIFICATION CANCELLED**",
                        description="You declined the rules. Type `!verify` to start again.",
                        color=VerificationEmbedBuilder.COLOR_ERROR
                    ))
                    return None
                responses[label] = "✅ Agreed to Terms & Rules"
                continue

            # ── Conditional question handling ────────────────────────────
            depends_on = q_data.get("depends_on_label")
            if depends_on:
                previous_answer = responses.get(depends_on, "").strip()
                if not previous_answer or previous_answer.lower() == "[skipped]":
                    continue
                # Word-boundary-safe negative check. The old substring check
                # matched 'na' inside 'natural'/'banana', 'not' inside 'noted',
                # etc. and skipped questions that should have been asked.
                if _is_negative_answer(previous_answer):
                    continue

            # ── Regular question ─────────────────────────────────────────
            embed = VerificationEmbedBuilder.question_screen(
            idx, total, brand_text(question), label, brand_text(hint) if hint else hint)
            embed.add_field(name="⏭️ Skip Option",
                                value="Type `skip` to skip this question", inline=False)
            await user.send(embed=embed)

            try:
                msg = await self.bot.wait_for(
                    'message',
                    check=lambda m: m.author == user and m.channel.type == discord.ChannelType.private,
                    timeout=self.config.timing.verification_timeout_seconds
                )

                content = msg.content.strip()

                if content.lower() == 'cancel':
                    await user.send(embed=VerificationEmbedBuilder.create_base_embed(
                        title="❌ **VERIFICATION CANCELLED**",
                        description="Type `!verify` to start again.",
                        color=VerificationEmbedBuilder.COLOR_ERROR
                    ))
                    return None

                if skip_allowed and content.lower() == 'skip':
                    responses[label] = "[Skipped]"
                    continue

                if accepts_attach and msg.attachments:
                    responses[label] = msg.attachments[0].url
                else:
                    responses[label] = content

            except asyncio.TimeoutError:
                await user.send(embed=VerificationEmbedBuilder.timeout())
                return None

        return responses

    async def _handle_agreement(self, user) -> bool:
        cached = self.data_manager.load_rules_cache()
        gang_rules  = cached.get('gang_rules', brand_text("Unable to fetch [GANG ABBR] rules. Contact staff."))
        server_rules = cached.get('server_rules', "Unable to fetch Server rules. Contact staff.")

        embed = VerificationEmbedBuilder.rules_intro()
        view  = ImprovedRulesView(user.id, gang_rules, server_rules, timeout=600)
        await user.send(embed=embed, view=view)
        await view.wait()
        return view.agreed

    async def _show_confirmation(self, user, responses: Dict[str, str]) -> bool:
        embed = VerificationEmbedBuilder.confirmation_screen(responses)
        view  = ImprovedConfirmationView(user.id, timeout=120)
        await user.send(embed=embed, view=view)
        await view.wait()
        return view.confirmed

    async def _submit_to_staff(self, ctx, user, responses: Dict[str, str]):
        now = datetime.now(timezone.utc)
        created_at = user.created_at.replace(tzinfo=timezone.utc) if user.created_at.tzinfo is None else user.created_at
        joined_at  = (user.joined_at.replace(tzinfo=timezone.utc)
                      if user.joined_at and user.joined_at.tzinfo is None else user.joined_at)

        account_info = {
            'account_age':    (now - created_at).days,
            'joined_age':     (now - joined_at).days if joined_at else 'Unknown',
            'created_at':     created_at.strftime('%Y-%m-%d'),
            'security_flags': self._get_security_flags(user)
        }

        embed = VerificationEmbedBuilder.staff_submission(user, responses, account_info)
        view  = ImprovedStaffActionView(user.id, user.display_name, embed)

        channel = self.bot.get_channel(self.config.channels.verification_submission)
        if channel:
            await channel.send(
                f"<@&{self.config.roles.verification_ping}> 📥 **New Application!**",
                embed=embed, view=view
            )

        await user.send(embed=VerificationEmbedBuilder.submission_complete())

    @staticmethod
    def _get_security_flags(user) -> List[str]:
        flags = []
        if not user.avatar:
            flags.append("No profile picture")
        if len(user.display_name) < 3:
            flags.append("Very short username")
        if user.display_name.isdigit():
            flags.append("Username is all numbers")
        now = datetime.now(timezone.utc)
        created_at = user.created_at.replace(tzinfo=timezone.utc) if user.created_at.tzinfo is None else user.created_at
        if (now - created_at).days < 30:
            flags.append("Account less than 30 days old")
        return flags

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # --- VERIFICATION COMMANDS ---
    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def verifyuser(ctx: commands.Context, member: discord.Member) -> None:
        role = ctx.guild.get_role(config.roles.verified)
        if role not in member.roles:
            await member.add_roles(role)
            await ctx.send(embed=EmbedBuilder.success("User Verified", f"Welcome {member.mention} to GNG! You have been verified."))
            logging.info(f'User {member} was verified by {ctx.author}')
        else:
            await ctx.send(f"{member.mention} is already verified.")


    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def securitycheck(ctx: commands.Context, member: discord.Member) -> None:
        embed = discord.Embed(title=f"Security Check: {member.display_name}", color=discord.Color.blue())
    
        now = datetime.now(timezone.utc)
        created_at = member.created_at.replace(tzinfo=timezone.utc) if member.created_at.tzinfo is None else member.created_at
        account_age = (now - created_at).days
    
        age_risk = "HIGH" if account_age < 13 else "MEDIUM" if account_age < 30 else "LOW"
        embed.add_field(name="Account Age", value=f"{account_age} days ({age_risk} risk)", inline=True)
    
        join_age = (now - member.joined_at.replace(tzinfo=timezone.utc)).days if member.joined_at else "Unknown"
        embed.add_field(name="Time in Server", value=f"{join_age} days", inline=True)
    
        profile_flags: List[str] = []
        if not member.avatar:
            profile_flags.append("No profile picture")
        if len(member.display_name) < 3:
            profile_flags.append("Very short username")
        if member.display_name.isdigit():
            profile_flags.append("Username is all numbers")
    
        profile_risk = "HIGH" if len(profile_flags) >= 2 else "MEDIUM" if profile_flags else "LOW"
        embed.add_field(name="Profile Risk", value=f"{profile_risk}\n{', '.join(profile_flags) if profile_flags else 'No flags'}", inline=False)
    
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
        embed.set_footer(text=f"User ID: {member.id}")
    
        await ctx.send(embed=embed)


    @bot.command()
    @commands.cooldown(1, 3600, commands.BucketType.user)
    async def verify(ctx: commands.Context) -> None:
        """
    Improved verification command — uses ImprovedVerificationSystem with
    modern embeds, button navigation, Roblox modal, and staff action view.
    The old text-based flow (_collect_verification_responses etc.) is replaced
    entirely; the legacy VerificationButtonsView is kept for any old submissions
    still open in the channel.
    """
        if not ows_get("enable_verification"):
            await ctx.send(embed=EmbedBuilder.warning(
                "🔐 Verification Disabled",
                "The verification system is currently disabled by the owner. Please try again later."
            ), delete_after=15)
            return

        if not ows_get("verification_cooldown"):
            verify.reset_cooldown(ctx)

        if ctx.author.id in process_manager._active_verifications:
            await ctx.send(
                embed=VerificationEmbedBuilder.create_base_embed(
                    title="⏳ **VERIFICATION IN PROGRESS**",
                    description="You already have an active verification session. Check your DMs!",
                    color=VerificationEmbedBuilder.COLOR_WARNING
                )
            )
            return

        system = ImprovedVerificationSystem(bot, config, data_manager)
        await system.start_verification(ctx)
