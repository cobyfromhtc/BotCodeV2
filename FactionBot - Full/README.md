# FactionBot — FullVersion

The **full-featured** edition of FactionBot in the SaaS-tier layout: a
slim 330-line entry point (`src/bot.py`) plus layered infrastructure
(`core/`, `config/`, `utils/`) and one feature module per domain
(`modules/`), with the two premium feature packages (`packages/`).

> Feature rule: anything added to the ShortVersion gets ported here (and
> vice-versa). See `../docs/FEATURES.md` for the parity matrix.

## Layout

```
FullVersion/
├── launchers/            Run_Bot.bat · run_bot.sh (self-creating venv)
├── src/
│   ├── bot.py            330-line entry: intents, TicketBot, registration
│   ├── config/           environment.py (token loading) · settings.py (Config)
│   ├── core/             paths · state · models · data_manager · lifecycle
│   │                     · ows · process_manager · domains · helpers
│   ├── modules/          feature modules by domain (register(bot) contract)
│   │   ├── administration/  owner tools + tutorial, setup wizard, branding,
│   │   │                   channel config, admin suite
│   │   ├── engagement/      giveaways, invite tracking, leveling, polls
│   │   ├── factions/        gang rules
│   │   ├── moderation/      moderation suite, message log, sticky roles
│   │   ├── runtime/         gateway event routing (on_message / on_ready / …)
│   │   ├── support/         info commands + tickets/ (engine, views, commands)
│   │   └── verification/    member verification flow
│   ├── packages/         tickettool/ (26 modules) · reactionroles/ (5) ·
│   │                     faction_access/ (8 — multi-guild licensing)
│   ├── utils/            ui/ (EmbedBuilder, paginated views, common helpers)
│   ├── data/             bot_data.db · JsonData/ · logs/ · backups/ · legacy/
│   └── assets/           emojis/ · images/ · templates/
├── .env.example
├── README.md
└── requirements.txt
```

**Command surface** — 161 top-level / 210 qualified prefix commands, 0
slash (prefix-only by design). Verified 1:1 against the original
33,403-line monolith: the ONLY deliberate deltas are `!sync` (deleted —
slash machinery is gone), `!botbranding name` (added as a parity port
from the Short variant) and the FactionAccess suite (`!license` group +
`!request`, v4.2.0). Everything else is byte-identical in names,
aliases, and permissions.

## What's inside

| System | Highlights |
| --- | --- |
| 🎫 Tickets | Panels (button/dropdown/reaction/multi), forms, claiming, automation engine, SLA, CSAT, escalation, transcripts, canned replies, knowledge base, analytics |
| ✅ Verification | Staff-review gang verification flow, pending-info states, staff action views |
| 🎁 Giveaways | Full giveaway system with auto-draw and restore-on-restart |
| 📊 Polls | Emoji-reaction polls (10–600 s) |
| 📈 Invites | Invite tracking, leaderboards, regeneration |
| ⭐ Leveling | XP curve, in-memory cache, level commands |
| 🛡 Moderation | Warnings + auto-ban, temp mutes, purge, lock, slowmode, blacklist engine, sticky roles, message logging, welcome messages, custom branding, OWS owner toggles |
| 🧩 Premium | tickettool (26 real modules) + reactionroles (5 real modules) |
| 🛡️ Multi-guild licensing | FactionAccess package (8 modules, byte-identical with the Short edition) — `!license` suite + `!request`, per-guild feature bundles, per-guild gang identity + bot nickname, expiry, audit trail |

## Requirements
- Python 3.10+
- A Discord bot token (**Server Members** + **Message Content** + **Presence** intents)

## Setup
1. Copy `.env.example` to `.env` and set your token:
   `BOT_TOKEN=YOUR_BOT_TOKEN_HERE`
2. Install dependencies:
   `pip install -r requirements.txt`
3. Start the bot:
   `python src/bot.py` (or use `launchers/`)

The SQLite database (`src/data/bot_data.db`) is created and migrated
automatically on first start — this variant's database is fully separate
from the ShortVersion's, and all paths anchor to this folder no matter
which directory you launch from.

## Architecture notes

- **Layering rule** — `core/` never imports `modules/`, `utils/` or
  `packages/` at module level (deferred, cycle-safe imports only).
- **register(bot) family contract** — every feature module exposes
  `register(bot)`; `src/bot.py` imports each entry from
  `modules.EXTENSIONS` and calls it once, right after the bot instance
  is created. `modules.runtime.events` uses `register_events(bot)` for
  the `@bot.event` handlers.
- **Shared state** — singletons live on `core.state` (`config`,
  `data_manager`); names that are REBOUND at runtime (`bot`,
  `ticket_tool`, caches) are always accessed as `state.<name>`.
- **Premium packages** — `tickettool` / `reactionroles` keep the
  `commands.register(bot)` + `wiring.on_setup_hook(dm, bot)` contract
  they had as inline sources; `from packages import tickettool as
  TicketTool` preserves the old import surface.
- **`cogs/tickets/` → `modules/support/tickets/`** — the ticket
  subpackage (engine / views / commands) stays a lazy, cycle-free
  subpackage.
