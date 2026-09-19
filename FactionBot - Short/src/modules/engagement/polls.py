# -*- coding: utf-8 -*-
"""Native Discord polls with lifecycle persistence for FactionBot.

Polls are created with discord.py's native ``discord.Poll`` support and
tracked in SQLite so the bot can:

* end polls on request (``!poll end <id>``) or automatically when due
  (60-second background sweep);
* extract and store final vote counts (defensively — the vote-count API
  surface differs between library versions);
* re-announce results, list polls, and cancel them.

All persistence lives in the shared SQLite database via :mod:`utils.botkit`.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple

import discord
from discord.ext import commands, tasks

from utils.botkit import (
    COLOR_BRAND,
    create_tables,
    error,
    fetchall,
    fetchone,
    get_conn,
    fmt_dt,
    fmt_duration,
    info,
    jdump,
    jload,
    now_iso,
    parse_iso,
    run,
    success,
    warning,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 10080  # 7 days
MIN_QUESTION_LENGTH = 1
MAX_QUESTION_LENGTH = 300  # Discord poll question limit
MAX_ANSWER_LENGTH = 55  # Discord poll answer limit
QUICK_POLL_MINUTES = 1440  # 24h default for quick polls
POLL_PAGE_SIZE = 10

_TABLES: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS polls (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id         INTEGER,
        channel_id       INTEGER,
        message_id       INTEGER,
        question         TEXT,
        options          TEXT,
        author_id        INTEGER,
        created_at       TEXT,
        ends_at          TEXT,
        duration_minutes INTEGER,
        multi_select     INTEGER DEFAULT 0,
        anonymous        INTEGER DEFAULT 1,
        status           TEXT DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS poll_results (
        poll_id     INTEGER PRIMARY KEY,
        results     TEXT,
        total_votes INTEGER,
        ended_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_polls_guild_status ON polls (guild_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_polls_ends ON polls (ends_at)",
)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class PollsCog(commands.Cog, name="Polls"):
    """Native Discord polls with stored results and auto-ending."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- lifecycle -----------------------------------------------------------
    async def cog_load(self) -> None:
        self._create_tables()
        if not self.check_due.is_running():
            self.check_due.start()

    def cog_unload(self) -> None:
        if self.check_due.is_running():
            self.check_due.cancel()

    # -- DB helpers (everything wrapped + logged) -----------------------------
    def _create_tables(self) -> None:
        try:
            create_tables(_TABLES)
        except Exception:
            log.exception("[Polls] could not create tables")

    def _db_run(self, sql: str, args: Tuple[Any, ...] = ()) -> bool:
        try:
            run(sql, args)
            return True
        except Exception:
            log.exception("[Polls] DB write failed: %s", sql.strip().split("\n")[0])
            return False

    def _db_fetchone(self, sql: str, args: Tuple[Any, ...] = ()):
        try:
            return fetchone(sql, args)
        except Exception:
            log.exception("[Polls] DB read failed: %s", sql.strip().split("\n")[0])
            return None

    def _db_fetchall(self, sql: str, args: Tuple[Any, ...] = ()):
        try:
            return fetchall(sql, args)
        except Exception:
            log.exception("[Polls] DB read failed: %s", sql.strip().split("\n")[0])
            return []

    def _fb_default(self, section: str, key: str, default: Any) -> Any:
        """Defensively read a default from the core config (bot.fb_config)."""
        fb_config = getattr(self.bot, "fb_config", None)
        if fb_config is None:
            return default
        try:
            section_obj = getattr(fb_config, section, None)
            if section_obj is None:
                return default
            value = getattr(section_obj, key, default)
        except Exception:
            return default
        return default if value is None else value

    def _option_bounds(self) -> Tuple[int, int]:
        raw_min = int(self._fb_default("limits", "min_poll_options", 2) or 2)
        raw_max = int(self._fb_default("limits", "max_poll_options", 10) or 10)
        # Discord native polls accept 2..10 answers — clamp config accordingly.
        minimum = min(max(raw_min, 2), 10)
        maximum = min(max(raw_max, minimum), 10)
        return minimum, maximum

    # -- poll construction -----------------------------------------------------
    def _build_poll(
        self, question: str, options: List[str], duration_minutes: int, multi: bool = False
    ) -> discord.Poll:
        poll = discord.Poll(
            question=discord.PollMedia(text=question),
            duration=timedelta(minutes=duration_minutes),
            multiple=multi,
        )
        for option in options:
            poll.add_answer(text=option)
        return poll

    def _build_quick_poll(self, question: str) -> discord.Poll:
        poll = discord.Poll(
            question=discord.PollMedia(text=question),
            duration=timedelta(minutes=QUICK_POLL_MINUTES),
            multiple=False,
        )
        poll.add_answer(text="Yes", emoji="✅")
        poll.add_answer(text="No", emoji="❌")
        return poll

    def _poll_info_embed(
        self, question: str, author: discord.abc.User, ends_at: Optional[datetime]
    ) -> discord.Embed:
        embed = info("Poll open", f"**{question}**")
        embed.set_footer(text=f"Ends {fmt_dt(ends_at)} • by {author.display_name}")
        return embed

    # -- result extraction (defensive across API versions) ----------------------
    async def _extract_results(self, message: discord.Message) -> List[Tuple[str, int]]:
        """Return [(label, votes), ...] for a poll message, robust to API drift."""
        results: List[Tuple[str, int]] = []
        poll = getattr(message, "poll", None)
        if poll is None:
            return results
        answers = getattr(poll, "answers", None)
        if not answers:
            # Some versions/contexts expose the answer list as `.results`.
            answers = getattr(poll, "results", None) or []
        try:
            answers = list(answers)
        except Exception:
            return results
        for index, answer in enumerate(answers, start=1):
            label = self._answer_label(answer, index)
            votes = await self._answer_votes(answer)
            results.append((label, votes))
        return results

    def _answer_label(self, answer: Any, index: int) -> str:
        media = getattr(answer, "media", None)
        if media is not None:
            for attr in ("text", "label"):
                value = getattr(media, attr, None)
                if isinstance(value, str) and value.strip():
                    return value
        value = getattr(answer, "text", None)
        if isinstance(value, str) and value.strip():
            return value
        return f"Option {index}"

    async def _answer_votes(self, answer: Any) -> int:
        # 1) Preferred: vote_count() — awaitable in some discord.py versions.
        try:
            value = answer.vote_count()
            if inspect.isawaitable(value):
                value = await value
            return int(value)
        except Exception:  # not the async-API shape — fall through to plain attributes
            log.debug("[Polls] vote_count() not awaitable-callable; using attribute fallback")
        # 2) Fallback: plain attribute / property (discord.py 2.7 uses this).
        for attr in ("vote_count", "count", "votes"):
            try:
                value = getattr(answer, attr)
            except Exception:
                continue
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        # 3) Give up — zero.
        return 0

    def _poll_finalized(self, poll: Any) -> bool:
        for name in ("is_finalised", "is_finalized"):
            checker = getattr(poll, name, None)
            if callable(checker):
                try:
                    return bool(checker())
                except Exception:
                    continue
        return False

    # -- results persistence -------------------------------------------------------
    def _store_results(self, row: Dict[str, Any], results: List[Tuple[str, int]], total: int) -> None:
        self._db_run(
            "INSERT OR REPLACE INTO poll_results (poll_id, results, total_votes, ended_at) VALUES (?,?,?,?)",
            (
                int(row["id"]),
                jdump([{"label": label, "votes": votes} for label, votes in results]),
                total,
                now_iso(),
            ),
        )

    def _mark_ended(self, row: Dict[str, Any]) -> None:
        self._db_run("UPDATE polls SET status = 'ended' WHERE id = ?", (int(row["id"]),))

    def _results_embed(
        self,
        row: Dict[str, Any],
        results: List[Tuple[str, int]],
        total: int,
        ended_by: Optional[discord.Member] = None,
        live: bool = False,
    ) -> discord.Embed:
        ordered = sorted(results, key=lambda item: item[1], reverse=True)
        peak = max((votes for _, votes in ordered), default=0)
        lines: List[str] = []
        for position, (label, votes) in enumerate(ordered, start=1):
            bar_length = int(round(votes / peak * 20)) if peak > 0 else 0
            percentage = (votes / total * 100) if total else 0.0
            crown = "🏆 " if position == 1 and votes > 0 else ""
            lines.append(
                f"{crown}**{label}** — {votes} vote(s) · {percentage:.1f}%\n"
                f"`{'█' * bar_length}{'░' * (20 - bar_length)}`"
            )
        description = "\n\n".join(lines) if lines else "*No votes were recorded.*"
        embed = discord.Embed(
            title=f"📊 {row['question']}",
            description=description,
            color=COLOR_BRAND,
        )
        embed.set_footer(text=f"Poll #{row['id']} • {total} total vote(s)")
        if live:
            embed.description = f"{description}\n\n*⏳ Live counts — approximate until the poll ends.*"
        if ended_by is not None:
            embed.add_field(name="Ended early by", value=ended_by.mention, inline=True)
        return embed

    # -- finalize pipeline -----------------------------------------------------------
    async def _finalize_poll(
        self, row: Dict[str, Any], *, announce: bool,
        ended_by: Optional[discord.Member] = None,
    ) -> Dict[str, Any]:
        """End a poll, store its results, and (optionally) announce them.

        Returns ``{"outcome": "ended"|"missing"|"failed"|"lost-race", ...}``.

        Concurrency: the poll's status is conditionally flipped from 'active'
        to 'ending' with a single UPDATE. If a concurrent `!poll cancel` or
        another sweep tick already claimed it, `rowcount` is 0 and we bail
        out — preventing double `poll.end()` calls (which Discord rejects
        with a 400) and duplicate results posts.
        """
        poll_id = int(row["id"])

        # Conditional claim — only one caller wins this row.
        try:
            conn = get_conn()
            try:
                cursor = conn.execute(
                    "UPDATE polls SET status = 'ending' "
                    "WHERE id = ? AND status = 'active'",
                    (poll_id,),
                )
                conn.commit()
                won = cursor.rowcount > 0
            finally:
                conn.close()
        except Exception as exc:
            log.warning("[Polls] conditional end-claim failed for %s: %s", poll_id, exc)
            return {"outcome": "failed", "results": [], "total": 0}

        if not won:
            return {"outcome": "lost-race", "results": [], "total": 0}

        channel_id = int(row.get("channel_id") or 0)
        message_id = int(row.get("message_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None and channel_id:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden,
                    discord.HTTPException, discord.ClientException):
                channel = None
        if channel is None or not hasattr(channel, "fetch_message"):
            self._mark_ended(row)
            self._store_results(row, [], 0)
            return {"outcome": "missing", "results": [], "total": 0}

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            self._mark_ended(row)
            self._store_results(row, [], 0)
            return {"outcome": "missing", "results": [], "total": 0}
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("[Polls] can't fetch message for poll %s: %s", poll_id, exc)
            self._mark_ended(row)
            return {"outcome": "failed", "results": [], "total": 0}

        poll = getattr(message, "poll", None)
        if poll is not None and not self._poll_finalized(poll):
            try:
                await poll.end()
            except (discord.ClientException, discord.Forbidden,
                    discord.HTTPException) as exc:
                log.info("[Polls] end() on poll %s: %s (continuing)", poll_id, exc)

        # Give the API a moment to publish final counts, then refetch.
        await asyncio.sleep(2)
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            self._mark_ended(row)
            self._store_results(row, [], 0)
            return {"outcome": "missing", "results": [], "total": 0}
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("[Polls] can't refetch message for poll %s: %s", poll_id, exc)

        results = await self._extract_results(message)
        total = sum(votes for _, votes in results)
        self._store_results(row, results, total)
        self._mark_ended(row)

        if announce:
            try:
                await channel.send(
                    embed=self._results_embed(row, results, total, ended_by=ended_by)
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "[Polls] couldn't announce results for poll %s: %s", poll_id, exc
                )
        return {"outcome": "ended", "results": results, "total": total}

    # -- background sweep --------------------------------------------------------------
    @tasks.loop(seconds=60)
    async def check_due(self) -> None:
        """Sweep every minute for polls whose end time has passed.

        Due polls are finalised in parallel (capped at 5 at once) because
        each `_finalize_poll` needs ~2s of API wait time — serialising 20
        due polls would stall the sweep for 40+ seconds.
        """
        rows = self._db_fetchall("SELECT * FROM polls WHERE status = 'active'")
        if not rows:
            return

        now = discord.utils.utcnow()
        due_rows: List[Dict[str, Any]] = []
        for row in rows:
            ends_at = parse_iso(row["ends_at"]) if "ends_at" in row.keys() else None
            if ends_at is None or ends_at > now:
                continue
            due_rows.append(dict(row))

        if not due_rows:
            return

        semaphore = asyncio.Semaphore(5)

        async def _run(row: Dict[str, Any]) -> None:
            async with semaphore:
                try:
                    await self._finalize_poll(row, announce=True)
                except Exception:
                    log.exception("[Polls] failed to finalize poll %s", row.get("id"))

        await asyncio.gather(*(_run(r) for r in due_rows))

    @check_due.before_loop
    async def before_check_due(self) -> None:
        await self.bot.wait_until_ready()

    # -- command group --------------------------------------------------------------------
    @commands.group(name="poll")
    @commands.guild_only()
    async def poll_group(self, ctx: commands.Context) -> None:
        """Create and manage native polls."""
        prefix = ctx.clean_prefix
        embed = info("Polls", "Native Discord polls, tracked end-to-end by FactionBot.")
        embed.add_field(
            name="Subcommands",
            value=(
                f"`{prefix}poll create <minutes> <question> | <opt1> | <opt2> | ...`\n"
                f"`{prefix}poll quick <question>` (Yes/No, 24h)\n"
                f"`{prefix}poll end <id>` · `{prefix}poll cancel <id>` · "
                f"`{prefix}poll results <id>` · `{prefix}poll list [active|all]`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    # -- create ------------------------------------------------------------------------------
    @poll_group.command(name="create")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def poll_create(self, ctx: commands.Context, minutes: int, *, question: str) -> None:
        """Create a poll: minutes, then "question | option1 | option2 ..."."""
        if minutes < MIN_DURATION_MINUTES or minutes > MAX_DURATION_MINUTES:
            await ctx.send(
                embed=error(
                    "Invalid duration",
                    f"Duration must be between **{MIN_DURATION_MINUTES}** and "
                    f"**{MAX_DURATION_MINUTES}** minutes (7 days).",
                )
            )
            return

        segments = [segment.strip() for segment in question.split("|")]
        question_text = segments[0] if segments else ""
        options = [segment for segment in segments[1:] if segment]
        if len(question_text) < MIN_QUESTION_LENGTH:
            await ctx.send(embed=error("Missing question", "Provide a question before the first `|`."))
            return
        if len(question_text) > MAX_QUESTION_LENGTH:
            await ctx.send(
                embed=error(
                    "Question too long", f"Questions are limited to {MAX_QUESTION_LENGTH} characters."
                )
            )
            return
        for option in options:
            if len(option) > MAX_ANSWER_LENGTH:
                await ctx.send(
                    embed=error(
                        "Option too long",
                        f"`{option}` — poll answers are limited to {MAX_ANSWER_LENGTH} characters.",
                    )
                )
                return
        minimum, maximum = self._option_bounds()
        if len(options) < minimum:
            await ctx.send(
                embed=error(
                    "Not enough options",
                    f"A poll needs at least **{minimum}** options.\n"
                    f"Usage: `{ctx.clean_prefix}poll create {minutes} Question | Option 1 | Option 2`",
                )
            )
            return
        if len(options) > maximum:
            await ctx.send(
                embed=error(
                    "Too many options",
                    f"A poll can have at most **{maximum}** options (you gave {len(options)}).",
                )
            )
            return

        poll = self._build_poll(question_text, options, minutes)
        ends_at = discord.utils.utcnow() + timedelta(minutes=minutes)
        try:
            message = await ctx.channel.send(
                poll=poll, embed=self._poll_info_embed(question_text, ctx.author, ends_at)
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(
                embed=error(
                    "Couldn't create poll",
                    f"Discord rejected the poll: {exc}\n"
                    "*(Note: the API may require whole-hour durations for some polls.)*",
                )
            )
            return

        stored = self._db_run(
            "INSERT INTO polls (guild_id, channel_id, message_id, question, options, author_id, "
            "created_at, ends_at, duration_minutes, multi_select, anonymous, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'active')",
            (
                ctx.guild.id,
                ctx.channel.id,
                message.id,
                question_text,
                jdump(options),
                ctx.author.id,
                now_iso(),
                ends_at.isoformat(),
                minutes,
                0,
                1,
            ),
        )
        if not stored:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("[Polls] couldn't delete untracked poll message %s: %s", message.id, exc)
            await ctx.send(embed=error("Database error", "The poll was posted but couldn't be tracked — try again."))
            return

        row = self._db_fetchone(
            "SELECT id FROM polls WHERE guild_id = ? AND message_id = ? ORDER BY id DESC LIMIT 1",
            (ctx.guild.id, message.id),
        )
        poll_id = int(row["id"]) if row is not None else 0
        await ctx.send(
            embed=success(
                "Poll created",
                f"Poll **#{poll_id}** is live in {ctx.channel.mention}.\n"
                f"**{question_text}** · {len(options)} options · runs {fmt_duration(minutes * 60)} "
                f"(ends {fmt_dt(ends_at)}).",
            )
        )

    # -- quick --------------------------------------------------------------------------------
    @poll_group.command(name="quick")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def poll_quick(self, ctx: commands.Context, *, question: str) -> None:
        """Quick Yes/No poll running for 24 hours."""
        question = question.strip()
        if not question:
            await ctx.send(embed=error("Missing question", "Ask something after `poll quick`."))
            return
        if len(question) > MAX_QUESTION_LENGTH:
            await ctx.send(
                embed=error("Question too long", f"Questions are limited to {MAX_QUESTION_LENGTH} characters.")
            )
            return

        poll = self._build_quick_poll(question)
        ends_at = discord.utils.utcnow() + timedelta(minutes=QUICK_POLL_MINUTES)
        try:
            message = await ctx.channel.send(
                poll=poll, embed=self._poll_info_embed(question, ctx.author, ends_at)
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(embed=error("Couldn't create poll", f"Discord rejected the poll: {exc}"))
            return

        stored = self._db_run(
            "INSERT INTO polls (guild_id, channel_id, message_id, question, options, author_id, "
            "created_at, ends_at, duration_minutes, multi_select, anonymous, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'active')",
            (
                ctx.guild.id,
                ctx.channel.id,
                message.id,
                question,
                jdump(["Yes", "No"]),
                ctx.author.id,
                now_iso(),
                ends_at.isoformat(),
                QUICK_POLL_MINUTES,
                0,
                1,
            ),
        )
        if not stored:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("[Polls] couldn't delete untracked poll message %s: %s", message.id, exc)
            await ctx.send(embed=error("Database error", "The poll was posted but couldn't be tracked — try again."))
            return

        row = self._db_fetchone(
            "SELECT id FROM polls WHERE guild_id = ? AND message_id = ? ORDER BY id DESC LIMIT 1",
            (ctx.guild.id, message.id),
        )
        poll_id = int(row["id"]) if row is not None else 0
        await ctx.send(
            embed=success(
                "Quick poll created",
                f"Poll **#{poll_id}** (Yes/No) is live in {ctx.channel.mention} and ends {fmt_dt(ends_at)}.",
            )
        )

    # -- end ------------------------------------------------------------------------------------
    @poll_group.command(name="end")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def poll_end(self, ctx: commands.Context, poll_id: int) -> None:
        """End an active poll now and record its results."""
        row = self._db_fetchone(
            "SELECT * FROM polls WHERE id = ? AND guild_id = ?", (poll_id, ctx.guild.id)
        )
        if row is None:
            await ctx.send(embed=error("Poll not found", f"No poll **#{poll_id}** exists in this server."))
            return
        if row["status"] != "active":
            await ctx.send(
                embed=error("Already finished", f"Poll **#{poll_id}** is already **{row['status']}**.")
            )
            return
        if int(row["author_id"] or 0) != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(
                embed=error(
                    "Not allowed",
                    "Only the poll's author (or someone with **Manage Server**) can end it early.",
                )
            )
            return

        if ctx.interaction is not None:
            await ctx.defer()
        outcome = await self._finalize_poll(dict(row), announce=True, ended_by=ctx.author)
        if outcome["outcome"] == "ended":
            await ctx.send(
                embed=success(
                    "Poll ended",
                    f"Poll **#{poll_id}** finished with **{outcome['total']}** vote(s) — results posted above.",
                )
            )
        elif outcome["outcome"] == "missing":
            await ctx.send(
                embed=warning(
                    "Poll message gone",
                    f"Poll **#{poll_id}**'s message was deleted — marked ended with no recorded results.",
                )
            )
        else:
            await ctx.send(
                embed=error(
                    "Couldn't end poll",
                    f"Discord refused to let me touch poll **#{poll_id}** — it stays active and I'll retry "
                    "on the next sweep.",
                )
            )

    # -- cancel ------------------------------------------------------------------------------------
    @poll_group.command(name="cancel")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def poll_cancel(self, ctx: commands.Context, poll_id: int) -> None:
        """Cancel an active poll and delete its message."""
        row = self._db_fetchone(
            "SELECT * FROM polls WHERE id = ? AND guild_id = ?", (poll_id, ctx.guild.id)
        )
        if row is None:
            await ctx.send(embed=error("Poll not found", f"No poll **#{poll_id}** exists in this server."))
            return
        if row["status"] != "active":
            await ctx.send(
                embed=error("Already finished", f"Poll **#{poll_id}** is already **{row['status']}**.")
            )
            return
        if int(row["author_id"] or 0) != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(
                embed=error(
                    "Not allowed",
                    "Only the poll's author (or someone with **Manage Server**) can cancel it.",
                )
            )
            return

        channel = self.bot.get_channel(int(row["channel_id"] or 0))
        deleted = True
        if channel is not None:
            try:
                message = await channel.fetch_message(int(row["message_id"] or 0))
                await message.delete()
            except discord.NotFound:
                deleted = True  # Already gone — nothing to delete.
            except (discord.Forbidden, discord.HTTPException) as exc:
                deleted = False
                log.warning("[Polls] couldn't delete message for poll %s: %s", poll_id, exc)
        else:
            deleted = True  # Channel gone → nothing to delete.

        self._db_run("UPDATE polls SET status = 'cancelled' WHERE id = ?", (poll_id,))
        description = f"Poll **#{poll_id}** ({row['question']}) is cancelled."
        if not deleted:
            description += "\n⚠️ I couldn't delete the poll message — remove it manually."
        await ctx.send(embed=success("Poll cancelled", description))

    # -- results -------------------------------------------------------------------------------------
    @poll_group.command(name="results")
    @commands.guild_only()
    async def poll_results(self, ctx: commands.Context, poll_id: int) -> None:
        """Show a poll's results (stored, or live if still running)."""
        row = self._db_fetchone(
            "SELECT * FROM polls WHERE id = ? AND guild_id = ?", (poll_id, ctx.guild.id)
        )
        if row is None:
            await ctx.send(embed=error("Poll not found", f"No poll **#{poll_id}** exists in this server."))
            return
        poll_row = dict(row)

        stored = self._db_fetchone("SELECT * FROM poll_results WHERE poll_id = ?", (poll_id,))
        if stored is not None:
            entries = jload(stored["results"], default=[]) or []
            results = [(str(entry.get("label", "?")), int(entry.get("votes", 0))) for entry in entries]
            total = int(stored["total_votes"] or 0)
            await ctx.send(embed=self._results_embed(poll_row, results, total))
            return

        if poll_row["status"] != "active":
            await ctx.send(
                embed=info(
                    "No results recorded",
                    f"Poll **#{poll_id}** is *{poll_row['status']}* but no results were stored "
                    "(its message may be gone).",
                )
            )
            return

        channel = self.bot.get_channel(int(poll_row.get("channel_id") or 0))
        if channel is None:
            await ctx.send(embed=error("Channel gone", "I can't find the poll's channel to fetch live results."))
            return
        try:
            message = await channel.fetch_message(int(poll_row.get("message_id") or 0))
        except discord.NotFound:
            await ctx.send(embed=error("Message gone", "The poll message was deleted — no live results available."))
            return
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(embed=error("Couldn't fetch", f"Discord refused to give me the message: {exc}"))
            return
        results = await self._extract_results(message)
        total = sum(votes for _, votes in results)
        await ctx.send(embed=self._results_embed(poll_row, results, total, live=True))

    # -- list -------------------------------------------------------------------------------------------
    @poll_group.command(name="list")
    @commands.guild_only()
    async def poll_list(self, ctx: commands.Context, scope: Literal["active", "all"] = "active") -> None:
        """List this server's polls (active by default)."""
        if scope == "all":
            rows = self._db_fetchall(
                "SELECT * FROM polls WHERE guild_id = ? ORDER BY id DESC", (ctx.guild.id,)
            )
        else:
            rows = self._db_fetchall(
                "SELECT * FROM polls WHERE guild_id = ? AND status = 'active' ORDER BY id DESC",
                (ctx.guild.id,),
            )
        if not rows:
            await ctx.send(
                embed=info(
                    "No polls",
                    "No polls here yet" if scope == "all" else "No active polls — try `poll list all`.",
                )
            )
            return
        paginator = PollListPaginator([dict(row) for row in rows], scope)
        await paginator.send(ctx)


# ---------------------------------------------------------------------------
# List paginator
# ---------------------------------------------------------------------------
class PollListPaginator(discord.ui.View):
    """Compact button paginator for the `poll list` command."""

    STATUS_ICONS = {"active": "🟢", "ended": "🏁", "cancelled": "🚫"}

    def __init__(self, rows: List[Dict[str, Any]], scope: str) -> None:
        super().__init__(timeout=180)
        self.rows = rows
        self.scope = scope
        self.page = 0
        self.page_count = max(1, (len(rows) + POLL_PAGE_SIZE - 1) // POLL_PAGE_SIZE)
        self.message: Optional[discord.Message] = None
        self._update_buttons()

    # -- rendering -----------------------------------------------------------
    def _embed(self) -> discord.Embed:
        start = self.page * POLL_PAGE_SIZE
        chunk = self.rows[start : start + POLL_PAGE_SIZE]
        lines: List[str] = []
        for row in chunk:
            icon = self.STATUS_ICONS.get(str(row.get("status")), "❔")
            author_id = int(row.get("author_id") or 0)
            author = f"<@{author_id}>" if author_id else "unknown"
            ends_at = parse_iso(row.get("ends_at"))
            if row.get("status") == "active":
                when = f"ends {fmt_dt(ends_at)}"
            else:
                when = str(row.get("status"))
            question = str(row.get("question") or "?")
            if len(question) > 80:
                question = f"{question[:77]}…"
            lines.append(f"{icon} **#{row.get('id')}** · {when} · by {author}\n> {question}")
        embed = info(f"Polls ({self.scope})", "\n".join(lines) or "*Nothing on this page.*")
        embed.set_footer(text=f"Page {self.page + 1}/{self.page_count} • {len(self.rows)} poll(s)")
        return embed

    def _update_buttons(self) -> None:
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1

    # -- buttons -----------------------------------------------------------
    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.page > 0:
            self.page -= 1
            self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page < self.page_count - 1:
            self.page += 1
            self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    # -- lifecycle -----------------------------------------------------------
    async def send(self, ctx: commands.Context) -> None:
        self.message = await ctx.send(embed=self._embed(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.debug("[Polls] couldn't disable paginator buttons: %s", exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PollsCog(bot))
