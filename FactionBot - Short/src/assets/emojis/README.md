# Emoji assets

`emoji_map.json` is the canonical emoji catalog — it mirrors the constants
the code uses (e.g. `PRIORITY_EMOJIS` in `modules/support/tickets/views.py`)
so themes and custom packs stay consistent across both variants.

Drop custom emoji image files (`.png` / `.gif`, named after the catalog
keys) here when wiring up a custom emoji pack.
