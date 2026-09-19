# -*- coding: utf-8 -*-
"""Generic paginated view shared by help/rules listings."""

# stdlib + discord.py
import discord
from datetime import datetime, timezone
from discord.ui import Button, View
from typing import List




# --- PAGINATED VIEW (V2 Enhancement) ---
class PaginatedView(View):
    def __init__(self, user_id: int, items: List[str], title: str, color: discord.Color, items_per_page: int = 5, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.items = items
        self.title = title
        self.color = color
        self.items_per_page = items_per_page
        self.current_page = 0
        self.total_pages = max(1, (len(items) + items_per_page - 1) // items_per_page)
        self._update_buttons()
    
    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1
    
    def _create_embed(self) -> discord.Embed:
        start = self.current_page * self.items_per_page
        end = start + self.items_per_page
        page_items = self.items[start:end]
        
        description = "\n\n".join(str(item) for item in page_items)
        
        embed = discord.Embed(title=self.title, description=description or "No items to display.", color=self.color)
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.total_pages}")
        embed.timestamp = datetime.now(timezone.utc)
        
        return embed
    
    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your menu!", ephemeral=True)
            return
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self._create_embed(), view=self)
        else:
            await interaction.response.defer()
    
    @discord.ui.button(label="▶️ Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your menu!", ephemeral=True)
            return
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self._create_embed(), view=self)
        else:
            await interaction.response.defer()
