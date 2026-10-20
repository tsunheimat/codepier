"""Atomic progress evidence for disposable service-acceptance processes only.

An observer may briefly hold a Windows file without delete sharing. Retry only
that bounded publication failure; never fabricate progress or silently accept a
permanent permissions error. No application credentials or user files are used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


def publish_progress(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.progress-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(50):
            try:
                os.replace(temporary, path)
                return
            except PermissionError as exc:
                # Windows readers or scanners can transiently deny replacement.
                # Do not retry ordinary POSIX permissions or unrelated failures.
                if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == 49:
                    raise
                time.sleep(.02)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
