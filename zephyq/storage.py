"""Append-only session event log (JSON Lines).

Every change to the queue is written here BEFORE it is applied in memory
(write-ahead).  The file therefore serves two purposes:

* it is the timestamped record of the session (submitted / called /
  resolved / cancelled times), used for the session summary and reports;
* after a server restart, replaying it rebuilds the exact queue state,
  including ticket numbers, so students can RESUME their tickets.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("zephyq.storage")


class EventLog:
    def __init__(self, path: str, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        self._fh = None
        if not readonly:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def read_all(self) -> List[Dict[str, Any]]:
        """Return all events.  A damaged line (e.g. power loss mid-write) is skipped."""
        events: List[Dict[str, Any]] = []
        if not os.path.exists(self.path):
            return events
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    log.warning("%s:%d: skipped damaged log line", self.path, lineno)
                    continue
                if isinstance(ev, dict) and "event" in ev:
                    events.append(ev)
        return events

    def append(self, event: Dict[str, Any]) -> None:
        if self.readonly:
            return
        if self._fh is None:
            needs_newline = False
            if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                with open(self.path, "rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    needs_newline = fh.read(1) != b"\n"     # damaged last line
            self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
            if needs_newline:
                self._fh.write("\n")
        self._fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())     # survive a crash right after the reply

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def archive(self, archive_dir: str) -> Optional[str]:
        """Move the current log to archive_dir and start an empty one."""
        self.close()
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return None
        os.makedirs(archive_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(self.path)))
        dest = os.path.join(archive_dir, "session-%s.jsonl" % stamp)
        n = 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(archive_dir, "session-%s-%d.jsonl" % (stamp, n))
        shutil.move(self.path, dest)
        return dest
