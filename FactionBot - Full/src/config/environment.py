# -*- coding: utf-8 -*-
"""Token loading — .env / tokens.txt resolution + placeholder detection.

Search anchors use ``core.paths`` so the variant root's ``.env`` is found
from ANY launch directory (this fixes a drift introduced when the token
loader moved a level deeper during the layered split: the ``..``
candidates resolved one directory too shallow instead of the variant
root).
"""

# stdlib + discord.py
import logging
import os
from typing import Optional

from core.paths import _PROJECT_ROOT, _SRC_ROOT





    # --- TOKEN & STARTUP (multi-bot aware) ---
def load_local_env_file(env_path: Optional[str] = None) -> None:
    """Load environment variables from a local .env file into os.environ.

    Search order (first existing file wins; later files are NOT merged in):
      1. An explicit ``env_path`` argument (if provided).
      2. ``.env`` at the variant root (``FullVersion/.env``).
      3. ``.env`` inside ``src/`` (``FullVersion/src/.env``).
      4. ``.env.example`` at the variant root — this is a documented fallback so
         users who paste a real token into the shipped template (a common
         beginner mistake) still get picked up. Placeholder values like
         ``YOUR_BOT_TOKEN_HERE`` are filtered out by ``is_placeholder_token``
         downstream, so a pristine template never accidentally "provides" a
         token.

    Only the FIRST file that exists is read, and only keys not already in
    os.environ are set (so real environment variables always win).
    """
    if env_path is None:
        candidate_paths = [
            os.path.join(_PROJECT_ROOT, '.env'),         # variant root .env (preferred)
            os.path.join(_SRC_ROOT, '.env'),             # src/.env (fallback)
            os.path.join(_PROJECT_ROOT, '.env.example'),  # variant root .env.example (last resort)
        ]
        env_path = None
        for path in candidate_paths:
            try:
                if os.path.exists(path):
                    env_path = path
                    break
            except Exception:
                continue
        if env_path is None:
            return

    try:
        if not os.path.exists(env_path):
            return

        # Log which file we're loading so the user can see where their token
        # came from (helpful when debugging "why isn't my token being read").
        short_name = os.path.basename(env_path)
        if short_name == '.env.example':
            logging.warning(
                "[Startup] Loading tokens from '.env.example' (fallback). "
                "Rename it to '.env' for a cleaner setup."
            )

        with open(env_path, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                # Skip blank lines, comments, and lines without a key=value pair.
                if not line or line.startswith('#') or '=' not in line:
                    continue

                key, value = line.split('=', 1)
                key = key.strip()
                # Strip inline comments (e.g. "BOT_TOKEN=xxx  # my main bot")
                # but only outside of quotes, so quoted values with '#' are safe.
                value = value.strip()
                if value and not (value.startswith('"') and value.endswith('"')) \
                        and not (value.startswith("'") and value.endswith("'")):
                    hash_idx = value.find(' #')
                    if hash_idx == -1:
                        hash_idx = value.find('\t#')
                    if hash_idx != -1:
                        value = value[:hash_idx].rstrip()
                value = value.strip().strip('"').strip("'")

                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:
        logging.warning(f"[Startup] Could not load env file {env_path}: {exc}")


def is_placeholder_token(token: Optional[str]) -> bool:
    if not token:
        return True

    normalized = token.strip().lower()
    return normalized in {"", "your_bot_token_here", "your_discord_bot_token_here",
                          "changeme", "replace_me", "bot_token_here"}


def _clean_token_value(raw: Optional[str]) -> Optional[str]:
    """Normalize a raw token value: take only the first whitespace-separated
    chunk (Discord tokens contain no spaces, so anything after whitespace is
    an inline comment/annotation like 'TOKEN_HERE  <- what this bot runs')."""
    if not raw:
        return None
    text = raw.strip().strip('"').strip("'").strip()
    if not text:
        return None
    parts = text.split()
    if len(parts) > 1:
        logging.warning("[Startup] Extra text after a token was ignored "
                        "(Discord tokens contain no spaces).")
    return parts[0]


def read_token_from_file(token_name: str = 'BOT_Token') -> Optional[str]:
    search_paths = [
        os.path.join(_PROJECT_ROOT, 'tokens.txt'),   # variant root (preferred)
        os.path.join(_SRC_ROOT, 'tokens.txt'),       # src/ (fallback)
        os.path.join(os.getcwd(), 'tokens.txt'),
        'tokens.txt',
    ]

    for filepath in search_paths:
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(f"{token_name}="):
                            value = _clean_token_value(line.split('=', 1)[1])
                            if value:
                                logging.info(f"Token found in: {os.path.abspath(filepath)}")
                                return value
        except Exception:
            continue

    return None


def get_bot_token() -> Optional[str]:
    load_local_env_file()

    for env_name in ('BOT_TOKEN', 'DISCORD_TOKEN', 'BOT_Token', 'TOKEN'):
        token_value = _clean_token_value(os.environ.get(env_name))
        if token_value and not is_placeholder_token(token_value):
            return token_value

    return read_token_from_file('BOT_Token')
