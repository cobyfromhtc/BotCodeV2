# FactionBot — ShortVersion

The **structured** edition of FactionBot in the SaaS-tier layout: a
`src/` tree with layered infrastructure (`config/`, `core/`, `utils/`)
plus one feature module per domain (`modules/`) and the two premium
feature packages (`packages/`). Same features as the FullVersion,
organized for maintainability — ideal for active development.

> Feature rule: anything added to the FullVersion gets ported here (and
> vice-versa). See `../docs/FEATURES.md` for the parity matrix.

## Layout

```
ShortVersion/
├── launchers/            Run_Bot.bat · run_bot.sh (self-locating)
├── src/
│   ├── bot.py            application layer: TicketBot, views, commands,
│   │                     events, tasks, multi-bot machinery, main()
│   ├── config/           environment.py (token loading) · settings.py (Config)
│   ├── core/             paths.py (anchors) · data_manager.py · state.py
│   ├── modules/          feature modules by domain (discord.py extensions,
│   │   ├── administration/  guided setup wizard
│   │   ├── engagement/      invites, leveling, polls
│   │   ├── factions/        reserved (Full-only feature; kept for parity)
│   │   ├── moderation/      automod rules engine
│   │   ├── support/         dynamic help system
│   │   └── verification/    member verification flow
│   ├── packages/         tickettool/ (26 modules) · reactionroles/ (5)
│   ├── utils/            botkit (thread-cached SQLite + embed/perm helpers)
│   ├── data/             bot_data.db · JsonData/ · logs/ · backups/ · legacy/
│   └── assets/           emojis/ · images/ · templates/
├── .env.example
├── README.md
└── requirements.txt
```

**What moved where** (SaaS restructure, Round 7): the 15,965-line single
`Bot.py` was decomposed — `Config` + its dataclasses →
`src/config/settings.py`, `DataManager` → `src/core/data_manager.py`,
the singletons → `src/core/state.py`, token loading →
`src/config/environment.py`, `botkit` → `src/utils/botkit.py`, the 7
subsystem cogs → `src/modules/<domain>/`. `src/bot.py` keeps ONLY the
application layer (13,158 lines) — verified with a **1:1 identical
command registry** (143 top-level / 226 qualified / 0 slash, before and
after).

## What's inside

| System | Highlights |
| --- | --- |
| 🎫 Tickets | Panels (button/dropdown/reaction/multi), forms, claiming, automation engine, SLA, CSAT, escalation, transcripts, canned replies, knowledge base, analytics |
| ✅ Verification | Persistent button panel, unverified/verified roles, account-age gate, timeout sweep (optional kick), join/verify logging |
| 📊 Polls | Native Discord polls, auto-ending, stored results with bar-chart announcements, history |
| 📈 Invites | Real join attribution (invite snapshots + vanity URLs), leaderboards, fake/bonus/left tracking, join announcements |
| ⭐ Leveling | MEE6-style XP curve, anti-farming (cooldown + duplicate detection), level role rewards, DB-backed |
| 🧩 Core | Reaction roles, sticky roles, warnings, temp mutes, message logging, welcome messages, custom commands, branding, OWS owner toggles, dynamic help, multi-command chaining |

## Requirements
- Python 3.10+
- A Discord bot token (with **Server Members** + **Message Content** +
  **Presence** intents)

## Setup
1. Copy `.env.example` to `.env` and fill in:
   `BOT_TOKEN=YOUR_BOT_TOKEN_HERE`
2. Install dependencies:
   `pip install -r requirements.txt`
3. Start the bot:
   `python src/bot.py` (or use `launchers/`)

The SQLite database (`src/data/bot_data.db`) is created and migrated
automatically on first start — this variant's database is fully separate
from the FullVersion's, and all paths anchor to this folder no matter
which directory you launch from.

## Architecture notes

- **Extension contract** — the subsystem modules are discord.py
  extensions (`async def setup(bot)`) loaded from `setup_hook` via
  `modules.EXTENSIONS`. They never import `src/bot.py`; everything they
  need is stashed on the bot (`bot.fb_config`, `bot.fb_data_manager`,
  `bot.embed_builder`) or lives in `utils/botkit`.
- **Layering rule** — `core/` and `config/` import only the standard
  library at module level. `settings.py` reaches the data layer through
  deferred imports (cycle-safe).
- **Premium packages** — `TicketTool` / `ReactionRoles` are real
  packages under `src/packages/`, imported as
  `from packages import tickettool as TicketTool`; they keep the
  `commands.register(bot)` + `wiring.on_setup_hook(dm, bot)` contract.
- **Timing loop registry** — `config.timing_appliers` (on the Config
  singleton) lets `!timingsetup` re-apply loop intervals without the
  extracted `settings.py` importing the entry module.
