# FactionBot — Performance Notes

Performance-relevant architecture notes for both editions, referenced from
`core/data_manager.py` docstrings and the in-Discord tutorials. This
document describes what the code actually does — no benchmarks are claimed
that were not measured.

## Data layer

**Storage.** One SQLite database per edition at `src/data/bot_data.db`,
opened in WAL mode. The two editions never share a database, log file or
instance lock (see *Startup discipline* below).

**DataManager (`core/data_manager.py`).** The persistence layer for both
editions is synchronous `sqlite3` guarded by a single `threading.Lock`,
fronted by caches so the hot paths stay in memory:

- **Read-through caches** cover the four reads that dominated per-message /
  per-command SQLite traffic:
  - `load_ticket_by_channel()` — ran on **every gateway message**; caches
    positive *and* negative entries (most channels are not ticket channels),
  - `get_message_log_config()` — ran on every logged message,
  - `load_ticket_settings()` — ran on every ticket flow and loop tick,
  - `get_branding()` — ran on every branded embed build.
  All cached reads return copies, so callers may safely mutate and re-save.
- **Generation counter.** Every write to the cached tables
  (`tickets`, `ticket_settings`, `message_log_config`, `bot_branding`)
  bumps a generation and clears the caches. A read that started before a
  write refuses to install its now-stale result — this closes the
  read-modify-install race against worker threads. Full clears are used
  deliberately: those writes are rare compared to message events, so a
  clear is cheaper and safer than per-entry bookkeeping.
- **Write-behind message buffer.** `message_log_cache` rows were formerly
  committed one per message; they are now buffered in memory and committed
  in a single transaction by a dedicated flusher thread. Logged messages
  never wait on a synchronous disk write.

## Hot-path optimizations

- **tickettool JSON fast-paths** (`packages/tickettool/db.py`).
  `_json_loads_list()` and `_json_loads_dict()` short-circuit the two most
  common stored values (`''` / `'[]'` and `''` / `'{}'`), so `json.loads` —
  and the surrounding isinstance checks — only run when the string could
  actually contain JSON. Both also self-heal rows that were written
  double-encoded.
- **tickettool staff-stats write batching**
  (`packages/tickettool/analytics.py`). `_write_staff_stats_cache()`
  persists the computed staff aggregates **once per
  `build_staff_report()` invocation**. The report builders themselves stay
  pure read paths, so a `!staffstats` command never blocks the event loop
  on N synchronous SQLite writes.
- **Short `utils/botkit.py`** — thread-local cached SQLite connections
  (`get_conn()` / `run()` / `fetchall()` / `fetchone()`): one connection
  per thread, held for the process lifetime, with WAL + `busy_timeout` +
  `synchronous=NORMAL` pragmas. Short's modules use botkit as their data
  toolkit. Where a module must run a synchronous read from `async def`
  (e.g. the AutoMod periodic rule-cache refresh), it dispatches through
  `asyncio.to_thread` so the event loop stays responsive.
- **Full `utils/ui/`** — `EmbedBuilder` plus paginated views centralize
  embed construction, so branding lookups hit the DataManager branding
  cache once per build instead of being re-derived per field.

## Startup discipline

- **`setup_hook` runs exactly once.** Database initialization and cache
  hydration live in `setup_hook`, *not* in `on_ready` — `on_ready` can
  re-fire on gateway reconnects, and re-running schema creation / hydration
  there would waste work and race the running bot. This invariant holds in
  both editions.
- **Cache hydration at startup.** In-memory caches (blacklist, AutoMod
  rules, giveaways, levels, …) are hydrated from SQLite once during
  startup, so steady-state gateway events read memory, not disk.
- **Self-anchoring paths** (`core/paths.py`). Every runtime path resolves
  from the `_SRC_ROOT` / `_PROJECT_ROOT` / `_DATA_DIR` anchors, so an
  edition works regardless of the CWD it is launched from — and can never
  resolve into the sibling edition's `data/` directory. Databases, logs
  and the single-instance lock file are all edition-scoped this way.

## Why synchronous SQLite

The DataManager is, honestly described, synchronous `sqlite3` behind a
`threading.Lock` — not an async driver. The design trade:

- The lock serializes writers, and for this single-server bot's write
  volume that contention is negligible.
- The read-through caches keep the per-message and per-command hot paths
  in memory, so the event loop almost never pays for a synchronous query
  in the first place.
- The write-behind buffer keeps the highest-frequency writer (message
  logging) off the interactive path entirely.
- `aiosqlite` is pinned in `requirements.txt` and probed for availability
  (`SQLITE_AVAILABLE`) by the application layer, but the DataManager
  itself does not depend on it.

The known cost: a synchronous query that misses every cache does block the
event loop briefly. The caches exist precisely to make those misses rare;
any new hot-path read should go through the same cache-invalidate pattern
rather than adding ad-hoc queries.

---

Related documents: `docs/FEATURES.md` · `docs/CHANGELOG.md`
