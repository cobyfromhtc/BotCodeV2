# -*- coding: utf-8 -*-
"""Path anchors — every runtime file location resolves from HERE.

All data/log paths are anchored to this file's real location so the bot
always reads/writes inside its own project folder, no matter the CWD it
was launched from. Both variants keep fully separate databases and log
files.

SaaS layout anchors:

    .../ShortVersion/src/core/paths.py
    _HERE         .../ShortVersion/src/core
    _SRC_ROOT     .../ShortVersion/src          (source + data root)
    _PROJECT_ROOT .../ShortVersion              (variant root: .env, launchers)
    _DATA_DIR     .../ShortVersion/src/data     (DB, JSON data, logs, backups)
"""
import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../src
_SRC_ROOT = _HERE                                                     # .../src
_PROJECT_ROOT = os.path.dirname(_SRC_ROOT)                             # variant root
_DATA_DIR = os.path.join(_SRC_ROOT, "data")                            # src/data
