# Changelog

All notable changes to FactionBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and
each entry is written from verifiable records only. **This changelog begins
with the first verifiable restructure rounds** — history before Short
"Round 7" and Full "Round 4" predates it and is not individually documented.
Where a date was not recorded, the entry says so instead of inventing one.

The two editions carry independent version lines and are versioned together
when they change together: Short is on the 5.x line, Full on the 4.x line.
Entries below apply to both editions unless the heading names one.

## [5.2.1 / 4.2.1] — 2026-09-19

Full "Round 6.1" — `!regenerateinvites` reliability fix (the command could
appear permanently stuck on "🔄 Regenerating Invites"), plus a latent
lazy-import defect shared by both editions.

### Fixed — Full (`modules/engagement/invites.py`)

- **`!regenerateinvites` "stuck" fix.** The delete phase ran one
  rate-limited HTTP delete per invite with no progress feedback and no
  logging on success, so a guild with an accumulated backlog of bot-created
  invites showed a frozen progress embed for minutes. The delete loop now
  (a) shows a live counter — `⏳ Deleting old invite links… (X/Y)` —
  refreshed at most every 3 s, (b) deletes in bounded 5-invite concurrent
  chunks (~5× faster through backlogs), and (c) reports a final
  `Deleted X/Y` line before minting the new batch.
- **Root cause of the backlog: `regenerate_expired` (the 5-minute auto
  task) minted replacement batches but never deleted the replaced invites
  from Discord**, so live bot invites accumulated without bound (the
  live DB already tracked 8 batches / 24 codes). It now deletes the
  replaced codes Discord-side (best-effort, non-fatal), archives only
  after minting succeeds (a broken mint no longer re-archives the same
  dead batch every cycle), and prunes the tracked set to a 24-entry cap
  (Discord's 25-field embed limit; oldest expired entries first).
- **Frozen-embed failure modes.** `progress_msg.edit`, `channel.create_invite`
  and `update_invite_message`'s `fetch_message` had gaps where a single
  HTTP failure (message deleted, invite-cap 400, lost Read Message
  History) killed the coroutine mid-run and left the embed frozen forever.
  All progress updates are now best-effort with a fresh-message fallback,
  `create_invite` skips failing channels with a per-channel warning, and
  the whole command is wrapped so any unexpected error ends as
  `❌ Regeneration failed: …` on the embed plus a logged traceback.
- **Check-task efficiency.** `check_and_update_all` fetched the entire
  guild invite list once per tracked code (24 codes = 24 full fetches per
  5-minute cycle); it now fetches once per cycle and passes the list down.
  A failed fetch skips the cycle instead of marking every invite expired.

### Fixed — both editions

- `modules/<domain>/__init__.py` lazy loaders hardcoded
  `modules.<domain>.verification` instead of `modules.<domain>.{name}` in
  all 12 non-placeholder domain packages, so any `from modules.engagement
  import invites`-style import raised `ModuleNotFoundError`. The bot itself
  imported submodules via `importlib.import_module` (which bypasses the
  package `__getattr__`) and never hit it — but the defect broke tooling
  and any future direct imports.

## [5.2.0 / 4.2.0] — 2026-09-19

Short "Round 9" / Full "Round 6" — FactionAccess: multi-guild licensing,
per-guild identity and feature-bundle gating as a dedicated premium-grade
package.

### Added

- `packages/faction_access/` (8 modules, ~2,600 lines) in BOTH editions —
  **byte-identical across editions** (md5-verified): `db` (4 `faction_*`
  tables + settings KV), `catalog` (feature-bundle classification,
  default-deny), `identity` (per-guild gang tag/name/bot nickname +
  substitution), `service` (licensing lifecycle, expiry sweeper, authority,
  notifications, audit), `gating` (the global command gate), `commands`
  (`!license` group + `!request`), `wiring` (lifecycle hooks).
- `!license` management suite: `pending` / `approve` / `deny` / `revoke` /
  `suspend` / `resume` / `list` / `info` / `grant` / `ungrant` / `identity` /
  `expiry` / `home` / `authority` / `invite` / `audit` / `catalog` — all
  authority-gated (application owner + runtime-editable allowlist, never a
  hardcoded id) and audit-logged.
- `!request` — allied-leader hotline to the license authority
  (manage_guild-gated, 5-minute per-user cooldown).
- `!license invite <guild>` — pre-scoped OAuth2 invite link generator
  (explicit least-privilege permission set, no Administrator).
- Global command gate (`bot.add_check`) — the first global check in either
  edition: DMs unchanged, home faction full access, always-available set
  (`!help` / `!cmds` / `!ping` / `!license` / `!request`), licensed guilds
  limited to granted bundles, everything else home-only **by default**;
  denial notices are self-announcing (the hosts' error handlers swallow
  generic `CheckFailure`) and throttled per (guild, user).
- `on_guild_join` / `on_guild_remove` handling (previously unclaimed in both
  editions): join requests land **pending** + DM the authority; leaves are
  recorded (re-join requires re-approval).
- License expiry sweeper — 30-minute `tasks.loop` that auto-suspends lapsed
  licenses and DMs the authority.
- Per-guild identity: bot **nickname** per server (the home faction's
  configured name at home, each ally's own name in its guild) + per-guild
  `[GANG NAME]` / `[GANG ABBR]` substitution in branded embed footers
  (`EmbedBuilder.branded`, both editions) and the welcome embed (Full).
- Tutorial **Page 8 — FactionAccess** in both editions (footers renumbered
  to /8); "New & Notable" gained the v5.2.0 / v4.2.0 entry.
- Readiness documentation: `docs/FEATURES.md` gained the Allied-guild system
  readiness matrix and the v1 identity limits.

### Changed

- Automation scope in both editions: home-config automations (welcome
  messages, blacklist scans + auto-ban, sticky-role restore, message-log
  caching, premium custom-command dispatch) are now **home-guild-only**;
  leveling XP follows the per-guild `leveling` bundle (Short: inside the
  leveling cog's listener; Full: in `on_message`). A missing FactionAccess
  service (pre-`setup_hook`) keeps the exact legacy behavior — the gate
  fails open for the home faction.
- `packages/__init__.py` (both editions): `faction_access` added to the
  package registry; contract docstring updated.
- Command surfaces grew by 2 top-level / 19 qualified commands each:
  Short 143→145 top-level / 226→245 qualified; Full 159→161 / 191→210
  (verified by import smoke test, discord.py 2.7.1).
- Versions: Short 5.1.0 → 5.2.0, Full 4.1.0 → 4.2.0 (`core/__init__.py`).

### Fixed

- Help listings now cover the FactionAccess commands (they were missing
  from the release): Full `!cmds` gained two "🤝 Faction Access" sections
  (license lifecycle + bundles/identity/authority + `!request`); Short
  `!help` gained a "🤝 Faction Access" category — the `!license` group,
  its 17 subcommands and `!request` were previously falling into
  "Utility & Misc" (and `!license resume` would have been miscategorized
  under Tickets). Example faction names in help/tutorial/docs were also
  replaced with generic placeholders (`ALLY` / "Ally Faction") — no
  concrete faction is named anywhere in source.


### Verification (real execution, /tmp copies with discord.py 2.7.1)

- Both editions import cleanly with the package registered; command counts
  match the numbers above exactly.
- Functional tests executed against both editions: classification of every
  sample command (incl. `botbranding name`/`avatar` → home-only via
  qualified-name overrides), full license lifecycle
  (approve/grant/ungrant/suspend/resume/expiry/revoke), identity resolution
  + per-guild substitution + fallback + reset, authority add/duplicate/remove,
  12 distinct audit actions recorded, scoped invite link, and every gate
  decision branch (home / DM / always / pending-deny+notice / throttle /
  granted / not-granted / home-only). 8-page tutorial built in both.
- All 8 package files md5-identical across editions; all modified files
  parse as Python 3.10-compatible.


## [5.1.0 / 4.1.0] — 2026-09-19

Short "Round 8" / Full "Round 5" — the SaaS quality pass: repository
hygiene, documentation, premium-package parity, version metadata.

### Added

- `.gitignore` at the repository root — secrets, databases, logs and
  bytecode are no longer version-controlled.
- `.env.example` in both editions (token template). The environment
  loader's documented last-resort fallback path now actually exists, and
  the missing-token error message points at it: `cp .env.example .env`.
- `launchers/run_bot.sh` for the Full edition — launcher parity with
  Short (self-locating, self-creating venv).
- `docs/FEATURES.md`, `docs/CHANGELOG.md`, `docs/PERFORMANCE.md` at the
  repository root, fixing every previously broken cross-reference (both
  edition READMEs, both `src/core/__init__.py`, `core/data_manager.py`,
  `modules/__init__.py` and the reserved `modules/factions/` package all
  pointed at files that did not exist).
- `EDITION` constant in both `src/core/__init__.py` files; professional
  startup banner now shows edition + version on launch.
- In-Discord tutorial (both editions): a "SaaS-quality repository pass"
  entry on the New & Notable page.

### Changed

- Version metadata: Short bumped to 5.1.0, Full bumped to 4.1.0.
- User-facing footers now show edition + version: the dynamic `!help`
  footer (Short) and the paginated `!cmds` footer (Full).
- Premium package parity sync, direction per file:
  - Full ← Short: `packages/tickettool/analytics.py`, `db.py`, `flows.py`,
    `commands.py` and `packages/reactionroles/commands.py` (Short held the
    newer micro-fixes: bulk staff-stats cache write, JSON parse fast-paths,
    hybrid-safe ephemeral responses, `invoke_without_command`).
  - Short ← Full: `packages/tickettool/__init__.py` and
    `packages/reactionroles/__init__.py` (Full held the better docstrings).
  - After the sync, both premium packages are byte-identical across the
    two editions (md5-verified).
- Tutorial Page 1 prerequisites corrected: the bot is prefix-only, so the
  `applications.commands` scope is no longer listed as a requirement.

### Fixed

- Help-text bug in `packages/tickettool/commands.py`: `"/panels"` now
  correctly reads `"!panels"` (fixed in both editions by the package sync).

### Removed

- Runtime artifacts taken out of version control — the files remain on
  disk, only untracked now: `.env` ×2, `bot_data.db` ×2 plus their WAL/SHM
  sidecars, `bot.log`, and 79 `__pycache__/*.pyc`.
- Stale `cpython-314` bytecode additionally deleted from disk.

## [5.0.0] — Short — the SaaS restructure (Round 7)

Date not recorded — predates this changelog and the repository import.

- Decomposed the 15,965-line single-`Bot.py` monolith into the layered
  `src/` tree: `Config` + its dataclasses → `config/settings.py`,
  `DataManager` → `core/data_manager.py`, the singletons →
  `core/state.py`, token loading → `config/environment.py`, `botkit` →
  `utils/botkit.py`, and the 7 subsystem cogs → `modules/<domain>/`.
- `src/bot.py` keeps only the application layer.
- Verified with a **1:1 identical command registry**: 143 top-level /
  226 qualified / 0 slash commands, before and after.

## [4.0.0] — Full — the SaaS package split (Round 4)

Date not recorded — predates this changelog and the repository import.

- The 33,403-line monolith became a 299-line entry point (`src/bot.py`)
  plus layered infrastructure (`config/`, `core/`, `utils/`) and 16
  `register(bot)` feature modules under `modules/`.
- Command surface verified **0-delta** against the original monolith, with
  exactly two deliberate changes: `!sync` deleted (the slash-command
  machinery is gone — the bot is prefix-only by design) and
  `!botbranding name` added (a parity port from the Short variant).

---

Related documents: `docs/FEATURES.md` · `docs/PERFORMANCE.md`
