# Runtime data — `src/data/`

Everything the bot writes at runtime lives here, anchored per variant
(see `core/paths.py`), so FullVersion and ShortVersion never share data.

| Path            | Purpose                                                  |
|-----------------|----------------------------------------------------------|
| `bot_data.db`   | SQLite database (tickets, panels, warnings, OWS, …)      |
| `JsonData/`     | JSON exports + legacy config files (auto-created)        |
| `logs/`         | rotating `bot.log` / `bot-<instance>.log`                |
| `backups/`      | manual + future automated DB backups (gitignored)        |
| `legacy/`       | archived pre-SQLite `.json.bak` files                    |

The lock file (`bot_busy.lock`) is also created here while a run is busy.
