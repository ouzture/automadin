"""Cross-platform subprocess options for background command-line tools."""

from __future__ import annotations

import subprocess
import sys


HIDDEN_PROCESS_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
)
