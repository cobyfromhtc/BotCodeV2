# FactionBot

**FactionBot** is a Discord bot built with [`discord.py`](https://discordpy.readthedocs.io/) — a full community-management suite covering tickets, verification, moderation, engagement, and premium tooling. Single-server by default, with a built-in **multi-guild licensing system** (`packages/faction_access/`) that lets the bot owner grant allied factions selective access to the same bot. This repository contains **two editions of the same bot** that are maintained in parallel and kept feature-synchronized:

| Edition | What it is |
| --- | --- |
| **`FactionBot - Short/`** | The *essentials* edition — exactly what a faction server needs: ticket system, verification, polling, invite management/tracking, leveling/XP, and general AutoMod, plus the supporting helpers. Structured as a layered `src/` tree with a large application layer (`src/bot.py`, ~13k lines). |
| **`FactionBot - Full/`** | The *full-featured* edition — everything Short has **plus** giveaways, gang/server rules, the owner OWS panel, channel auto-purge, branding module, admin suite, message logging, and sticky roles. Slim ~300-line entry point with everything decomposed into `core/` infrastructure and `register(bot)` feature modules. |

Both editions ship the same premium feature packages and read the same command surface style: **prefix commands only (`!`), zero slash commands, by design.**

> **Feature parity rule** — anything added to one edition gets ported to the other (or an equivalent exists, with the difference documented). The only intended differences are the scope split above and the structural layout. See [`docs/FEATURES.md`](docs/FEATURES.md) for the canonical parity matrix.

---

## Repository layout

```
FactionBot/
├── docs/                   FEATURES.md (parity matrix) · CHANGELOG.md ·
│                           PERFORMANCE.md
├── .gitignore              ignores secrets, runtime data, bytecode
├── FactionBot - Short/
│   ├── launchers/            Run_Bot.bat · run_bot.sh (self-creating venv)
│   ├── src/
│   │   ├── bot.py            application layer: TicketBot, views, commands,
│   │   │                     events, tasks, multi-bot machinery, main()
│   │   ├── config/           environment.py (token loading) · settings.py (Config)
│   │   ├── core/             paths.py · data_manager.py · state.py
│   │   ├── modules/          feature modules (discord.py extensions)
│   │   │                     verification · engagement · moderation ·
│   │   │                     administration · support · factions (reserved)
│   │   ├── packages/         tickettool/ (26 modules) · reactionroles/ (5) ·
│   │   │                     faction_access/ (8 — multi-guild licensing)
│   │   ├── utils/            botkit (thread-cached SQLite + embed/perm helpers)
│   │   ├── data/             bot_data.db · logs/ · legacy/
│   │   └── assets/           emojis/ · images/ · templates/
│   ├── .env.example         committed token template (copy to .env)
│   └── requirements.txt
└── FactionBot - Full/
    ├── launchers/            Run_Bot.bat · run_bot.sh (self-creating venv)
    ├── src/
    │   ├── bot.py            330-line entry: intents, TicketBot, registration
    │   ├── config/           environment.py · settings.py
    │   ├── core/             paths · state · models · data_manager ·
    │   │                     lifecycle · ows · process_manager · domains ·
    │   │                     helpers
    │   ├── modules/          feature modules (register(bot) contract)
    │   │                     administration · engagement · factions ·
    │   │                     moderation · runtime · support · verification
    │   ├── packages/         tickettool/ (26 modules) · reactionroles/ (5) ·
    │   │                     faction_access/ (8 — multi-guild licensing)
    │   ├── utils/            ui/ (EmbedBuilder, paginated views, helpers)
    │   ├── data/             bot_data.db · logs/ · legacy/
    │   └── assets/           emojis/ · images/ · templates/
    ├── .env.example         committed token template (copy to .env)
    └── requirements.txt
```

Each edition is fully self-contained: its own database, log file, and lock file. Paths anchor to the edition's own folder no matter which directory you launch from, so the two editions can never accidentally share state.

---

## Features

| System | Highlights |
| --- | --- |
| 🎫 **Tickets** | Panels (button / dropdown / reaction / multi), forms, claiming, automation engine, SLA targets, CSAT ratings, escalation routes, HTML transcripts, canned replies, knowledge base, staff analytics |
| ✅ **Verification** | **Short:** persistent button panel, unverified/verified roles, account-age gate, timeout sweep (optional kick) · **Full:** staff-review gang verification flow with pending-info states and staff action views |
| 🛡 **Moderation** | Warnings + auto-ban, temp mutes, purge, channel lock, slowmode, keyword blacklist engine, sticky roles (restore on rejoin), message logging, welcome messages |
| 🤖 **AutoMod** *(Short)* | Data-driven rules engine — 11 rule types, priority ordering, combined actions, strike tracking |
| 🎁 **Giveaways** *(Full)* | Full giveaway system with auto-draw and restore-on-restart |
| 📊 **Polls** | **Short:** native Discord polls, auto-ending, stored results with bar-chart announcements · **Full:** emoji-reaction polls with live bars |
| 📈 **Invites** | **Short:** real join attribution (invite snapshots + vanity URLs), leaderboards, fake/bonus/left tracking · **Full:** invite tracking, leaderboards, regeneration |
| ⭐ **Leveling** | MEE6-style XP curve, anti-farming (cooldown + duplicate detection), level role rewards |
| 🧩 **Reaction roles** | Premium package — reaction/button role panels, quick-add modal, up to 250 mappings |
| 🛡️ **Multi-guild licensing** | FactionAccess package — license the same bot to allied factions with per-guild feature bundles, per-guild gang identity + bot nickname, expiry, audit trail, `!license` management suite and `!request` allied hotline (both editions, byte-identical package) |
| 🧱 **Core** | Dynamic help, custom commands, per-guild branding, OWS owner toggles, multi-command chaining |

*(Edition-specific rows are marked; unmarked rows exist in both.)*

---

## Requirements

- **Python 3.10+**
- A **Discord bot token** with the following **Privileged Gateway Intents** enabled in the Developer Portal:
  - Server Members
  - Message Content
  - Presence
- Dependencies: `discord.py >= 2.4.0`, `aiosqlite >= 0.20.0` (see each edition's `requirements.txt`)

---

## Getting started

The setup is identical for both editions — just work inside the edition folder you want to run.

### 1. Create the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create an application.
2. Under **Bot → Reset Token**, generate a token. Keep it secret — anyone who has it can control the bot.
3. Under **Bot → Privileged Gateway Intents**, enable **Server Members**, **Message Content**, and **Presence**.
4. Invite the bot to your server via **OAuth2 → URL Generator** with the `bot` scope and the permissions your features need (the in-Discord setup wizard will guide you on channel/role requirements).

### 2. Configure the token

Copy the committed template and paste your token:

```bash
cp .env.example .env   # inside the edition folder
```

```env
BOT_TOKEN=your-bot-token-here
```

The token loader (`src/config/environment.py`) checks, in order:

1. `.env` at the edition root *(preferred)*
2. `.env` inside `src/`
3. `.env.example` at the edition root *(fallback)*
4. A `tokens.txt` file containing `BOT_Token=your-token`
5. Real environment variables always win over files

Placeholder values such as `YOUR_BOT_TOKEN_HERE` are detected and rejected at startup with instructions.

### 3. Install dependencies

```bash
cd "FactionBot - Short"        # or "FactionBot - Full"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # Linux / macOS
.venv\Scripts\pip install -r requirements.txt  # Windows
```

### 4. Run the bot

| Edition | Windows | Linux / macOS |
| --- | --- | --- |
| **Short** | `launchers\Run_Bot.bat` *(requires `.venv` to exist)* | `launchers/run_bot.sh` *(auto-creates the venv on first run)* |
| **Full** | `launchers\Run_Bot.bat` *(requires `.venv` to exist)* | `launchers/run_bot.sh` *(auto-creates the venv on first run)* |

Or directly from the edition folder: `python src/bot.py`.

The SQLite database (`src/data/bot_data.db`) is created and migrated automatically on first start — no manual schema setup.

### 5. First-run configuration (in Discord)

Configuration is stored in the database and managed through Discord commands — no config files to edit:

| Command | Purpose |
| --- | --- |
| `!abrev <name>` | Set your gang/community name (default `[GANG NAME]`) |
| `!channelsetup` | Guided channel configuration |
| `!rolesetup` | Guided role configuration |
| `!timingsetup` | Loop/timing configuration |
| `!limitssetup` | Limits configuration |
| `!serversetup` | Server/guild settings *(Full edition)* |
| `!license` | Multi-guild licensing management *(both editions — see below)* |
| `!tutorial` | Owner tutorial DMs |
| `!settings` / `!csetup` | Setup status overview *(Short)* |

Command prefix is `!` (both editions). Run `!help` for the command list — Short ships a dynamic replacement help system; Full keeps the built-in `!help` plus a paginated `!cmds` overview.

---

## Command surface

- **Prefix-only** — all commands use `!`, with aliases and subcommands (e.g. `!panel`, `!automod add`, `!rr add`).
- **Zero slash commands by design.**
- Approximate top-level command counts: **~145** (Short) / **~161** (Full), spanning tickets, panels, moderation, automod, engagement, administration, the two premium packages, and the FactionAccess licensing suite.
- Administrative commands enforce Discord permission checks (`manage_guild`, `administrator`, `moderate_members`, `manage_messages`, `manage_roles`, …) plus role/staff checks where configured.

---

## Multi-guild licensing (FactionAccess)

Both editions ship `packages/faction_access/` — one **byte-identical 8-module package** (md5-verified) that turns the single-server bot into a licensable multi-guild service while the purchaser keeps control:

- **Guild licensing lifecycle** — when the bot joins a new guild it lands **pending**; the license authority approves (`!license approve <guild> [bundle …]`), suspends, resumes or revokes it. Optional **expiry** (`!license expiry <guild> 30d`) auto-suspends via a background sweeper.
- **Feature bundles** — per-guild grants (`verification`, `tickets`, `reactionroles`, `moderation`, `automod`, `engagement`, `leveling`, `general`, `branding`). A single global check gates every prefix command; unclassified commands are **home-only by default (default-deny)**, and `!license catalog` reports drift.
- **Per-guild identity** — `!license identity <guild> tag ALLY name "Ally Faction" display "ALLY Moderation"` sets the gang tag/name and the bot's **per-server nickname**; embed footers rebrand per guild. The global gang name, avatar and presence stay with the home faction.
- **License authority** — the application owner plus a **runtime-editable allowlist** (`!license authority add`) — never a hardcoded user id.
- **Onboarding tooling** — `!license invite <guild>` generates a pre-scoped OAuth2 invite link; join requests and `!request` (allied-leader hotline, rate-limited) DM the authority; every action lands in the audit trail (`!license audit`).

Which systems genuinely *work* per allied guild differs by edition — see the readiness matrix in [`docs/FEATURES.md`](docs/FEATURES.md).

---

## Architecture overview

Both editions share the same layered philosophy — only the split point differs:

| Layer | Short | Full |
| --- | --- | --- |
| Entry point | `src/bot.py` — large application layer (views, commands, events, tasks inline) | `src/bot.py` — slim entry; everything lives in modules |
| Feature modules | discord.py **extensions** (`async def setup(bot)`), loaded via `await bot.load_extension(...)` in `setup_hook` | **`register(bot)` contract** — modules register synchronously after bot creation; `modules.runtime.events` installs `@bot.event` handlers |
| Core infrastructure | `core/paths · state · data_manager` (+ `ProcessManager`, `EmbedBuilder`, lifecycle inline in `bot.py`) | `core/paths · state · models · data_manager · lifecycle · ows · process_manager · domains · helpers` |
| Configuration | `config/settings.py` (`Config` persisting JSON blobs in the `bot_config` table) + `config/environment.py` (tokens) | same, plus `ServerConfig` and extra timing/limit fields |
| Premium packages | `packages/tickettool` (26 modules) + `packages/reactionroles` (5) — `commands.register(bot)` + `wiring.on_setup_hook(dm, bot)` contract | same packages, same contract |

Key invariants:

- **Layering rule** — `core/` and `config/` import only the standard library at module level; deeper layers are reached through deferred, cycle-safe imports.
- **`setup_hook` runs exactly once** — DB connection, cache hydration, and persistent-view registration happen there (not `on_ready`, which can re-fire on reconnects).
- **Self-anchoring paths** — every path resolves from the file's real location, so each edition always uses its own `src/data/` regardless of the launch directory.
- **Single-server by default, multi-guild by license** — the bot's configuration is bound to one home faction; additional guilds run under the FactionAccess gate with only granted bundles (home-config automations like welcome messages and blacklist scans never run in allied guilds).

---

## Data, logs, and paths

Per edition:

- **Database:** `src/data/bot_data.db` (SQLite, WAL mode) — tickets, panels, warnings, levels, invites, branding, config, premium tables. Auto-created and auto-migrated.
- **Logs:** `src/data/logs/bot.log` — rotating (1 MB × 5 backups). Set the `FACTIONBOT_INSTANCE` environment variable to get an instance-suffixed log file (`bot-<instance>.log`) when running multiple instances.
- **Backups / legacy:** `src/data/legacy/` holds `.bak` copies created by the optional JSON → SQLite import.

---

## Current parity status

The canonical parity matrix now lives in [`docs/FEATURES.md`](docs/FEATURES.md) (referenced by both edition READMEs and the code). Summary of the verified state:

- **By design (Full-only, beyond Short's essential scope):** giveaways, gang/server rules, owner OWS panel, channel auto-purge manager, branding module, admin suite. *(Short implements several of these inline in `bot.py`; giveaways and rules have no Short counterpart by design.)*
- **Genuine open parity items:** the AutoMod rules engine ships in Short only (Full has the simpler keyword blacklist); polls, invites, leveling, and verification exist in both editions with divergent implementations — see the matrix for the differences.
- **Premium packages:** `tickettool` (26 modules) and `reactionroles` (5 modules) are **byte-identical across editions** (md5-verified) after the parity sync in the [4.1.0 / 5.1.0] quality pass.

Any new feature must be implemented in **both** editions (or an equivalent, with the difference explained in `docs/FEATURES.md`).

---

## Development notes

- **Never commit secrets.** `.env` and `tokens.txt` are git-ignored; `.env.example` is the committed template. A real `BOT_TOKEN` must stay out of version control.
- **Never re-clone over the working copy.** Once local development begins, this repository's `FactionBot/` folder is the source of truth; sync with the upstream deliberately, diff-first.
- **Runtime artifacts stay out of version control.** `.gitignore` covers `__pycache__/`, databases (`bot_data.db*`), logs, and virtualenvs — all auto-created on first start, so a fresh checkout is fully functional.
- **Versioning:** each edition carries `EDITION` + `__version__` on `core/` (Short 5.1.0, Full 4.1.0), shown in the startup banner and the `!cmds` / `!help` footers. History lives in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).
- Both editions must be reviewed together for changes touching shared code (`packages/`, `config/environment.py`, `core/data_manager.py`).

---

## Credits

- Bot by **IdkAnymore_039**
- Upstream source: [`cobyfromhtc/BotCodeV2`](https://github.com/cobyfromhtc/BotCodeV2)
- Built on [discord.py](https://github.com/Rapptz/discord.py)
