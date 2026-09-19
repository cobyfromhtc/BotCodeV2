# -*- coding: utf-8 -*-
"""ProcessManager — verification busy-tracking + lock file."""

# stdlib + discord.py
import logging
import os
from datetime import datetime
from typing import Set

from core.state import config
from core.ows import ows_get




# --- PROCESS MANAGER ---
class ProcessManager:
    def __init__(self):
        self._active_verifications: Set[int] = set()
        self._active_processes: int = 0
    
    def add_verification(self, user_id: int) -> None:
        self._active_verifications.add(user_id)
        self._update_lock_file()
    
    def remove_verification(self, user_id: int) -> None:
        self._active_verifications.discard(user_id)
        self._update_lock_file()
    
    def is_busy(self) -> bool:
        return len(self._active_verifications) > 0 or self._active_processes > 0
    
    def _update_lock_file(self) -> None:
        if not ows_get("process_lock_file"):
            return
        try:
            if self.is_busy():
                with open(config.lock_file, 'w') as f:
                    f.write(f"busy since: {datetime.now().isoformat()}")
            else:
                if os.path.exists(config.lock_file):
                    os.remove(config.lock_file)
        except Exception as e:
            logging.error(f"Error updating lock file: {e}")
    
    def clear_lock_file(self) -> None:
        try:
            if os.path.exists(config.lock_file):
                os.remove(config.lock_file)
        except Exception as e:
            logging.error(f"Error removing lock file: {e}")


process_manager = ProcessManager()
