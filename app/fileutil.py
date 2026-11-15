"""Crash-safe file helpers.

WHY this module exists (AUDIT H5): the original code saved state with
``Path.write_text()``, which truncates the file *before* writing. A crash or
power cut in between left an empty or half-written file, and the loaders then
quietly treated that as "no state", which wiped reply cooldowns and dedup.

``atomic_write_text`` writes to a temp file in the same folder, fsyncs it and
then ``os.replace``-s it over the target. ``os.replace`` is atomic on Windows
and POSIX, so other readers only ever see the complete old file or the
complete new file.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable


def _replace_with_retry(src: str, dst: Path, attempts: int = 5) -> None:
    # WHY retry: on Windows os.replace raises PermissionError while another
    # process (Excel with the CSV open, antivirus, backup tools) briefly holds
    # the target file. A short retry fixes most of those cases; if it still
    # fails we raise instead of silently skipping the write.
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` atomically (never leaves a partial file)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        # newline="" so callers control line endings exactly (the csv module
        # already writes \r\n; JSON uses \n).
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, data: object) -> None:
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_paths(paths: Iterable[Path | str], backup_root: Path | str, label: str) -> Path:
    """Copy every existing path (file or folder) into a new timestamped folder.

    Returns the backup folder. Raises if any copy fails. WHY raise: callers use
    this right before deleting or rewriting data (AUDIT H7), and a failed
    backup must stop the destructive step instead of letting it continue.
    """
    root = Path(backup_root)
    base = root / f"{label}-{timestamp_slug()}"
    dest = base
    counter = 1
    while dest.exists():
        dest = Path(f"{base}-{counter}")
        counter += 1
    dest.mkdir(parents=True)

    cwd = Path.cwd().resolve()
    for raw in paths:
        source = Path(raw)
        if not source.exists():
            continue
        resolved = source.resolve()
        try:
            # Keep the relative layout (data/x.csv, logs/x.log) so two files
            # with the same name in different folders don't overwrite each other.
            relative = resolved.relative_to(cwd)
        except ValueError:
            relative = Path(resolved.name)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if resolved.is_dir():
            shutil.copytree(resolved, target)
        else:
            shutil.copy2(resolved, target)
    return dest
