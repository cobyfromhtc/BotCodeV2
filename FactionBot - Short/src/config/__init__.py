# -*- coding: utf-8 -*-
"""FactionBot configuration package — environment + persisted settings.

    environment.py  .env / tokens.txt token resolution + placeholder
                    detection (search anchored to the variant root).
    settings.py     ``Config`` — branding / channels / roles / timing /
                    limits settings, persisted in SQLite and hydrated
                    during setup_hook.

Canonical imports::

    from config import Config
    from config.environment import get_bot_token
"""
from config.environment import (
    get_bot_token,
    is_placeholder_token,
    load_local_env_file,
    read_token_from_file,
)
from config.settings import Config

__all__ = [
    "Config",
    "get_bot_token",
    "is_placeholder_token",
    "load_local_env_file",
    "read_token_from_file",
]
