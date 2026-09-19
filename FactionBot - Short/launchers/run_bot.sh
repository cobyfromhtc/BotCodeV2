#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# FactionBot - Linux/macOS launcher
#
# Activates the local .venv (creating it if missing) and starts the bot.
# Reads the token from .env or tokens.txt automatically (handled in src/bot.py).
# ---------------------------------------------------------------------------
set -euo pipefail

# Resolve the project root (one level up from this script's directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"

PYTHON_EXE="$PROJECT_DIR/.venv/bin/python"
BOT_FILE="$PROJECT_DIR/src/bot.py"

# Auto-create + populate the venv if it is missing so first-time runs work
# without manual setup.
if [ ! -x "$PYTHON_EXE" ]; then
    echo "Virtual environment not found. Creating it now..."
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PYTHON_EXE" -m pip install --upgrade pip
    "$PYTHON_EXE" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi

if [ ! -f "$BOT_FILE" ]; then
    echo "Bot file not found: $BOT_FILE"
    exit 1
fi

echo "Starting FactionBot..."
exec "$PYTHON_EXE" "$BOT_FILE"
