"""Throw-away folders for the test scripts: created like tempfile.mkdtemp and
removed again when the script exits, so runs no longer pile up hundreds of
folders in %TEMP%. Set KEEP_TEST_OUTPUT=1 to keep them for inspection."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import time


def _remove(path: str):
    # Windows: a server / worker the test started may still hold a file for a
    # moment after it was stopped - retry briefly, never fail the test over it
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.5)


_paths: list[str] = []


def cleanup_now():
    """Remove the folders right away - for scripts that end with os._exit(),
    which skips atexit handlers."""
    while _paths:
        _remove(_paths.pop())


def scratch_dir(prefix: str) -> str:
    path = tempfile.mkdtemp(prefix=prefix)
    if os.environ.get("KEEP_TEST_OUTPUT", "").strip().lower() not in ("1", "true", "yes"):
        _paths.append(path)
    return path


atexit.register(cleanup_now)
