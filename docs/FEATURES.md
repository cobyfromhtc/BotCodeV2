# FactionBot — Feature Parity Matrix

Canonical Short/Full feature matrix, referenced from both edition READMEs
(`../docs/FEATURES.md`), `src/core/__init__.py` and the in-Discord tutorials.
Paths in this document are relative to the repository root unless noted.

## Canonical scope rule

The project owner's definition, verbatim intent:

> **Short** ships the essentials a faction server needs — the ticket system,
> verification, polling, invite management/tracking, leveling/XP, and general
> AutoMod, plus the misc supporting helpers those systems rely on.
> **Full** ships all of the listed above **plus all other features** —
> giveaways, gang/server rules, the owner OWS panel, channel auto-purge,
> branding, the admin suite, message logging, sticky roles.

Two editions, one codebase family: `FactionBot - Short/` (core version 5.x)
and `FactionBot - Full/` (core version 4.x) are maintained in parallel.

## Parity rule

1. Any feature added to one edition must be ported to the other — or an
   equivalent must exist, with the difference documented in this matrix.
2. When a feature is beyond Short's essential scope (see the rule above), it
   lands in Full only **by design**. That is a deliberate split, not a gap.
3. Divergent-but-equivalent implementations are allowed (see the Notes
   column); the difference must be explained here, not discovered in prod.

## Feature matrix

| System | Short | Full | Notes |
| --- | :---: | :---: | --- |
| Ticket system | ✓ | ✓ | Both run on the premium `packages/tickettool/` (26 modules). Short hosts `TicketToolSystem` + 40+ ticket commands **inline in `src/bot.py`**; Full ships a `modules/support/tickets/` subpackage (engine / views / commands, 41 ticket commands). Equivalent capability, different hosting. |
| Reaction roles | ✓ | ✓ | Premium `packages/reactionroles/` (5 modules). Byte-identical across editions since the 2026-09-19 parity sync. |
| Verification | ✓ | ✓ | **Divergent implementations.** Short: persistent button panel, account-age gate, unverified role on join, timeout sweep with optional kick (`modules/verification/`). Full: staff-review gang verification flow V2 with pending-info states and staff action views. |
| Polls | ✓ | ✓ | **Divergent implementations.** Short: native `discord.Poll` with auto-end and stored results/history (`modules/engagement/polls`). Full: emoji-reaction polls, 10–600 s duration (`modules/engagement/polls`). |
| Invite management / tracking | ✓ | ✓ | **Divergent implementations.** Short: richer DB-backed attribution — `invite_codes` / invite config / `joins` / `stats` tables, vanity URLs, fake/bonus/left tracking, leaderboards (`modules/engagement/invites`). Full: `InviteManager` with tracking, leaderboards, and invite regeneration. |
| Leveling / XP | ✓ | ✓ | **Divergent implementations.** Short: MEE6-style XP curve with anti-farming (cooldown + duplicate detection) and level role rewards, DB tables `level_config` / `level_roles` (`modules/engagement/leveling`). Full: XP curve with in-memory level cache and level commands. |
| AutoMod | ✓ | ✗ | Short: full rules engine (`modules/moderation/automod`) — 11 rule types (words, regex, invites, links, spam, duplicates, mentions, caps, emojis, repeated chars, newlines), combinable actions (delete / warn / strike / timeout / kick / ban / notify), exemptions, priorities, escalation, XP penalty. Full: keyword blacklist only, inside `modules/moderation/moderation.py`. **The main genuine open parity item — see below.** |
| Moderation suite | ✓ | ✓ | Short: suite (kick / ban / mute / warn / blacklist, …) **inline in `src/bot.py`**. Full: `modules/moderation/moderation.py`, 24 commands incl. warnings with auto-ban. Equivalent capability, different hosting. |
| Giveaways | ✗ | ✓ | Full: `modules/engagement/giveaways` with auto-draw and restore-on-restart. Beyond Short's essentials — Full-only by design. |
| Gang / server rules | ✗ | ✓ | Full: `modules/factions/rules.py` (cached embeds, `!gangrules` / `!serverrules` and friends). Short keeps a reserved `modules/factions/` package for structural parity — see Open parity work. |
| OWS owner panel | ✓ | ✓ | Short: OWS owner toggles **inline in `src/bot.py`**. Full: `modules/administration/owner.py` panel plus the guided owner tutorial. Equivalent capability, different hosting. |
| Channel auto-purge | ✓ | ✓ | Short: verification-channel auto-purge manager plus `!purge` / `!purgeall` / `!nopurge` **inline in `src/bot.py`**. Full: `modules/administration/channels.py` (same command set). |
| Branding | ✓ | ✓ | Short: **inline in `src/bot.py`**. Full: `modules/administration/branding.py`. Both cache branding rows in the DataManager (see `docs/PERFORMANCE.md`). |
| Admin suite | ✓ | ✓ | Short: **inline in `src/bot.py`**. Full: `modules/administration/admin.py` (dbcleanup, shutdown, botstatus, auditlog). |
| Message logging | ✓ | ✓ | Short: **inline in `src/bot.py`**. Full: `modules/moderation/msglog.py`. Both buffer log rows write-behind (see `docs/PERFORMANCE.md`). |
| Sticky roles | ✓ | ✓ | Short: **inline in `src/bot.py`**. Full: `modules/moderation/sticky_roles.py`. |
| Setup wizard | ✓ | ✓ | Short: guided wizard `!csetup` / `!settings` (`modules/administration/setup`). Full: setup wizard (`modules/administration/setup`). |
| Help system | ✓ | ✓ | **Divergent-but-equivalent.** Short: dynamic `!help` replacing the built-in (`modules/support/help`). Full: default `!help` plus paginated `!cmds` and `!getallroles` (`modules/support/info`). |
| Multi-bot command domains | ✓ | ✓ | Infrastructure, not user-facing. Short: multi-bot domain machinery **inline in `src/bot.py`**. Full: `core/domains.py`. |
| Premium packages | ✓ | ✓ | `tickettool` (26 modules) + `reactionroles` (5 modules) in both editions; byte-identical, md5-verified 2026-09-19. |
| Multi-guild licensing (FactionAccess) | ✓ | ✓ | `packages/faction_access/` (8 modules) — **byte-identical across editions** (md5-verified). `!license` management suite + `!request` hotline, per-guild feature bundles, per-guild gang identity + bot nickname, license expiry sweeper, authority allowlist, audit trail. See the dedicated section below. |

"Inline in `src/bot.py`" = the feature ships inside Short's monolithic
application layer (`src/bot.py`, ≈13,400 lines) rather than a `modules/`
package. The capability exists in both editions; only the code location
differs.

## Command surface summary

- Prefix-only, command prefix `!` — **zero slash commands by design** in
  both editions (no `applications.commands` scope needed).
- Short: ≈145 top-level / 245 qualified commands.
- Full: ≈161 top-level / 210 qualified commands.

## Multi-guild licensing (FactionAccess)

`packages/faction_access/` (8 modules, byte-identical in both editions) turns
the single-server bot into a licensable multi-guild service: allied factions
add the same bot via a pre-scoped OAuth invite (`!license invite <guild>`),
land **pending**, and unlock only the feature bundles the license authority
grants (`!license approve <guild> verification`). A single global check gates
every prefix command — unclassified commands are **home-only by default
(default-deny)**. The bot's nickname and embed branding rebrand per guild via
`!license identity`, and every licensing action is audited.

Command gate behavior (identical in both editions):

- DM invocations: unchanged legacy behavior (allowed).
- Home faction guild: full access by definition.
- Always available everywhere (even pending guilds): `!help` / `!cmds` /
  `!ping` / `!license` / `!request`.
- Pending / suspended / revoked / unregistered guilds: everything else denied
  with a self-announcing, throttled notice.
- Licensed allied guilds: only granted bundles (plus the always-available set).

### Allied-guild system readiness

Licensing and gating are edition-identical, but how far a granted system
actually *functions* inside an allied guild depends on where that system
stores its config — a consequence of the two editions' histories:

| Granted bundle | Short (allied guild) | Full (allied guild) |
| --- | --- | --- |
| verification | ✅ fully per-guild (`verification_config` keyed by guild — the ally runs `!verification setup` in their own server) | ⚠️ home-bound flow: portal channels come from the global config; commands gate correctly but the flow targets the home setup |
| tickets | ⚠️ mostly per-guild (ticket/panel rows keyed by guild; category/log channels come from the global config) | ⚠️ same — per-guild ticket rows, global category/log channel config |
| moderation | ✅ acts on the invoking guild's members (log channels stay home) | ✅ same |
| automod | ✅ per-guild rules engine (rules/config keyed by guild) | — bundle is Short-only (catalog carries it for the day Full gets the engine) |
| engagement | ✅ polls are channel-local; invites fully per-guild (DB-backed) | ⚠️ polls channel-local; invites use the home-bound single panel; giveaways home-bound |
| leveling | ✅ XP keyed (user, guild) — allies with the grant earn XP independently | ✅ same |
| general / branding | ✅ per-guild info + `bot_branding` rows | ✅ same |

Home-config automations (welcome embeds, blacklist scans, sticky-role
restore, message-log caching) are **home-guild-only** by design — allied
guilds never receive unsolicited automation driven by the home faction's
global channel/role/keyword config. Leveling XP is the one bundle-following
automation (per-guild data, harmless, explicitly granted).

Known v1 identity limits (documented, deliberate): the **global** bot status
(presence) and the bot's **username/avatar** are Discord-account-wide and
cannot vary per guild; per-guild identity covers the nickname + embed
branding. Full's verification-portal embeds render with the global gang name
(the flow itself is home-bound). Short's shared botkit embed footer stays
global; allied guilds that want their own footer use `!botbranding footer`
(per-guild) or the identity default footer via the branded path.

## Shared platform

Both editions run on the same stack: Python 3.10+, `discord.py>=2.4.0`,
`aiosqlite>=0.20.0`, SQLite at `src/data/bot_data.db` (WAL mode) — one
database **per edition**, never shared. All runtime paths are self-anchoring
(`core/paths.py`), so an edition works regardless of the CWD it is launched
from and never touches the sibling's DB, logs or lock files.

## Open parity work

### Genuine scope gaps

1. **AutoMod rules engine → Full** *(the main one)*. Full's own scope
   definition — "all of the listed above" — includes general AutoMod, but
   today Full only has the keyword blacklist inside its moderation suite.
   Porting Short's `modules/moderation/automod` rules engine (11 rule types,
   combinable actions, exemptions, priorities, escalation, XP penalty) is
   the primary open parity item.

### Designated port candidates (Full-only by design today)

2. **Gang / server rules → Short**. The reserved
   `FactionBot - Short/src/modules/factions/` package — kept so both
   variants share an identical domain layout — designates this port (of
   Full's `modules/factions/rules.py`) as "the top item" of this section's
   port work. Beyond Short's essentials, so Full-only by design until
   ported.
3. **Giveaways → Short**. Same rule: beyond essentials, Full-only by design;
   port on request.

### Divergent-but-equivalent implementations (documented, accepted)

Both editions ship the feature; the implementations differ by design
history. Any future convergence should be a deliberate decision, not drift:

- **Polls** — native `discord.Poll` + stored results (Short) vs
  emoji-reaction polls (Full).
- **Invites** — richer DB-backed attribution with vanity URLs and
  fake/bonus tracking (Short) vs `InviteManager` + regeneration (Full).
- **Leveling** — anti-farming + level role rewards, DB-backed (Short) vs
  in-memory level cache (Full).
- **Verification** — button-panel flow with account-age gate and timeout
  sweep (Short) vs staff-review V2 with pending-info states (Full).

---

Related documents: `docs/CHANGELOG.md` · `docs/PERFORMANCE.md`
