# -*- coding: utf-8 -*-
'''
TicketTool.kb — In-Discord Knowledge Base + Search (Tier 2 Features #12 + #13).

A fully self-contained KB that lives inside Discord (no website, no external
service). Articles are stored in SQLite, organized by category, and searchable
via /kb search. Articles can be staff-only. Article views are counted so
owners can see what users actually look up.

Commands (registered in TicketTool.commands):
  /kb add <category> <title> <content>          — create an article
  /kb staffonly <article_id> <true|false>      — toggle staff-only visibility
  /kb remove <article_id>                       — soft-delete an article
  /kb list [category]                           — list articles
  /kb search <query>                            — full-text search
  /kb view <article_id>                          — view one article
  /kb category add <name> [description]         — create a category
  /kb category remove <category_id>             — remove a category
  /kb stats                                     — view-count statistics

Article suggestions (Ticket Tool premium feature): when a user creates a
ticket, the bot can suggest KB articles matching their subject/first message.
Implemented in TicketTool.wiring.on_ticket_create -> kb.suggest_for_ticket().
'''

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord

from .db import PremiumDB


# =====================================================================
# ARTICLE CRUD
# =====================================================================

def _new_id(prefix: str = 'kb') -> str:
    import uuid as _uuid
    return f"{prefix}-{_uuid.uuid4().hex[:8]}"


def create_article(pdb: PremiumDB, *, guild_id: int, category: Optional[str],
                   title: str, content: str, summary: Optional[str] = None,
                   keywords: Optional[str] = None, staff_only: bool = False,
                   attachment_urls: Optional[List[str]] = None,
                   created_by: int) -> str:
    article_id = _new_id()
    pdb.save_kb_article({
        'article_id': article_id,
        'guild_id': guild_id,
        'category': category,
        'title': title,
        'content': content,
        'summary': summary,
        'keywords': keywords,
        'staff_only': int(staff_only),
        'attachment_urls': ','.join((attachment_urls or [])[:5]),
        'view_count': 0,
        'created_by': created_by,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'is_active': 1,
    })
    return article_id


def update_article(pdb: PremiumDB, article_id: str, *,
                   title: Optional[str] = None, content: Optional[str] = None,
                   summary: Optional[str] = None, keywords: Optional[str] = None,
                   category: Optional[str] = None,
                   staff_only: Optional[bool] = None) -> bool:
    article = pdb.get_kb_article(article_id)
    if not article:
        return False
    if title is not None: article['title'] = title
    if content is not None: article['content'] = content
    if summary is not None: article['summary'] = summary
    if keywords is not None: article['keywords'] = keywords
    if category is not None: article['category'] = category
    if staff_only is not None: article['staff_only'] = int(staff_only)
    pdb.save_kb_article(article)
    return True


def delete_article(pdb: PremiumDB, article_id: str) -> bool:
    return pdb.delete_kb_article(article_id)


def get_article(pdb: PremiumDB, article_id: str, *, viewer_is_staff: bool = False) -> Optional[Dict]:
    article = pdb.get_kb_article(article_id)
    if not article:
        return None
    if article.get('staff_only') and not viewer_is_staff:
        return None
    # Increment view count (best-effort, non-blocking).
    try:
        pdb.increment_kb_view(article_id)
    except Exception:
        pass
    article['view_count'] = int(article.get('view_count') or 0) + 1
    return article


def list_articles(pdb: PremiumDB, guild_id: int, *,
                  category: Optional[str] = None,
                  viewer_is_staff: bool = False) -> List[Dict]:
    return pdb.list_kb_articles(guild_id, category=category,
                                 include_staff_only=viewer_is_staff)


# =====================================================================
# SEARCH
# =====================================================================

def _score_article(article: Dict, query: str) -> int:
    '''Simple relevance score: title match > keywords > summary > content.'''
    q = query.lower()
    score = 0
    title = (article.get('title') or '').lower()
    keywords = (article.get('keywords') or '').lower()
    summary = (article.get('summary') or '').lower()
    content = (article.get('content') or '').lower()
    if q in title:
        score += 100
    if title.startswith(q):
        score += 50
    for token in q.split():
        if token in title:
            score += 10
        if token in keywords:
            score += 8
        if token in summary:
            score += 4
        if token in content:
            score += 1
    return score


def search(pdb: PremiumDB, guild_id: int, query: str, *,
           viewer_is_staff: bool = False, limit: int = 10) -> List[Dict]:
    '''Search articles. Returns ranked results (best match first).'''
    if not query or not query.strip():
        return []
    # First do a SQL LIKE search to narrow the candidate set.
    candidates = pdb.search_kb_articles(guild_id, query.strip(),
                                         include_staff_only=viewer_is_staff)
    if not candidates:
        # Fallback: token-by-token OR search.
        candidates = pdb.list_kb_articles(guild_id, include_staff_only=viewer_is_staff)
    # Score and rank.
    scored = [(a, _score_article(a, query)) for a in candidates]
    scored = [(a, s) for a, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [a for a, _ in scored[:limit]]


# =====================================================================
# CATEGORIES
# =====================================================================

def create_category(pdb: PremiumDB, *, guild_id: int, name: str,
                     description: Optional[str] = None,
                     staff_only: bool = False) -> str:
    cat_id = _new_id('cat')
    pdb.save_kb_category({
        'category_id': cat_id,
        'guild_id': guild_id,
        'name': name,
        'description': description,
        'staff_only': int(staff_only),
    })
    return cat_id


def list_categories(pdb: PremiumDB, guild_id: int) -> List[Dict]:
    return pdb.list_kb_categories(guild_id)


# =====================================================================
# STATS
# =====================================================================

def stats(pdb: PremiumDB, guild_id: int) -> Dict:
    '''Aggregate KB statistics for /kb stats.'''
    articles = pdb.list_kb_articles(guild_id, include_staff_only=True)
    if not articles:
        return {'total': 0, 'categories': 0, 'total_views': 0, 'top_articles': []}
    categories = set(a.get('category') or 'Uncategorized' for a in articles)
    total_views = sum(int(a.get('view_count') or 0) for a in articles)
    top = sorted(articles, key=lambda a: int(a.get('view_count') or 0), reverse=True)[:5]
    return {
        'total': len(articles),
        'categories': len(categories),
        'total_views': total_views,
        'top_articles': [
            {'article_id': a.get('article_id'), 'title': a.get('title'),
             'views': int(a.get('view_count') or 0)}
            for a in top
        ],
    }


# =====================================================================
# TICKET SUGGESTIONS
# =====================================================================

def suggest_for_ticket(pdb: PremiumDB, *, guild_id: int,
                       subject: Optional[str] = None,
                       first_message: Optional[str] = None,
                       limit: int = 3) -> List[Dict]:
    '''Suggest KB articles relevant to a newly-created ticket.

    Called from TicketTool.wiring.on_ticket_create. Returns a list of articles
    (best match first) so the bot can post "While you wait, these articles
    might help:" in the ticket channel.
    '''
    query_bits = []
    if subject:
        query_bits.append(subject)
    if first_message:
        # Use the first ~200 chars of the first message.
        query_bits.append(first_message[:200])
    query = ' '.join(query_bits).strip()
    if not query:
        return []
    return search(pdb, guild_id, query, viewer_is_staff=False, limit=limit)


# =====================================================================
# EMBED BUILDERS
# =====================================================================

def build_article_embed(article: Dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📚 {article.get('title', 'Untitled')}",
        description=article.get('content', '')[:4000] or 'No content',
        color=discord.Color(0x5865F2),
        timestamp=datetime.now(timezone.utc),
    )
    if article.get('summary'):
        embed.add_field(name="Summary", value=article['summary'][:1024], inline=False)
    cat = article.get('category') or 'Uncategorized'
    embed.add_field(name="Category", value=cat, inline=True)
    embed.add_field(name="Views", value=str(article.get('view_count', 0)), inline=True)
    if article.get('staff_only'):
        embed.add_field(name="Visibility", value="🔒 Staff only", inline=True)
    if article.get('attachment_urls'):
        urls = [u for u in (article.get('attachment_urls') or '').split(',') if u]
        for u in urls[:5]:
            embed.add_field(name="Attachment", value=f"[link]({u})", inline=False)
    embed.set_footer(text=f"Article ID: {article.get('article_id')}")
    return embed


def build_list_embed(articles: List[Dict], title: str = "📚 Knowledge Base") -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple(),
                          timestamp=datetime.now(timezone.utc))
    if not articles:
        embed.description = "No articles found."
        return embed
    # Group by category for readability.
    by_cat: Dict[str, List[Dict]] = {}
    for a in articles:
        cat = a.get('category') or 'Uncategorized'
        by_cat.setdefault(cat, []).append(a)
    for cat, items in by_cat.items():
        lines = []
        for a in items[:15]:
            staff_badge = ' 🔒' if a.get('staff_only') else ''
            views = int(a.get('view_count') or 0)
            lines.append(f"`{a.get('article_id')}` — {a.get('title')}{staff_badge} ({views} views)")
        embed.add_field(name=f"{cat} ({len(items)})", value="\n".join(lines), inline=False)
    embed.set_footer(text="Use /kb view <article_id> to read an article.")
    return embed


def build_search_embed(results: List[Dict], query: str) -> discord.Embed:
    embed = discord.Embed(title=f"🔎 KB Search: \"{query}\"", color=discord.Color.green(),
                          timestamp=datetime.now(timezone.utc))
    if not results:
        embed.description = "No matching articles found."
        return embed
    lines = []
    for i, a in enumerate(results[:10], 1):
        staff_badge = ' 🔒' if a.get('staff_only') else ''
        summary = (a.get('summary') or a.get('content', '')[:80] + '...')[:120]
        lines.append(f"**{i}.** `{a.get('article_id')}` — {a.get('title')}{staff_badge}\n   {summary}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Use /kb view <article_id> to read an article.")
    return embed


def build_stats_embed(stats: Dict) -> discord.Embed:
    embed = discord.Embed(title="📊 Knowledge Base Stats", color=discord.Color.gold(),
                          timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Total articles", value=str(stats.get('total', 0)), inline=True)
    embed.add_field(name="Categories", value=str(stats.get('categories', 0)), inline=True)
    embed.add_field(name="Total views", value=str(stats.get('total_views', 0)), inline=True)
    top = stats.get('top_articles') or []
    if top:
        lines = [f"`{a['article_id']}` — {a['title']} ({a['views']} views)" for a in top]
        embed.add_field(name="Top articles", value="\n".join(lines), inline=False)
    return embed
