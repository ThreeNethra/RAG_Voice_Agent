"""Invite codes: who may call, what they may ask about, and how much.

A code is shown once when it is created; only its SHA-256 is stored. The groups on an invite are exactly the
document groups the engine's access rules use, so a friend with groups ["all-staff"] can never hear anything
that is restricted to finance or exec, whatever they say on the call.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from rag.service import Principal


def _h(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


class Invites:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.fail: dict = defaultdict(deque)       # peer -> recent failure times
        self.today = (time.strftime("%Y-%m-%d"), defaultdict(float))   # (date, invite hash -> minutes used)
        self.active: set = set()                   # invite hashes with a call in progress
        self.load()

    def load(self) -> None:
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"invites": {}}
        self._mtime = self.path.stat().st_mtime_ns if self.path.exists() else 0

    def refresh(self) -> None:
        """Pick up codes created or revoked from another process (manage.py) while the server is running."""
        try:
            m = self.path.stat().st_mtime_ns if self.path.exists() else 0
        except OSError:
            return
        if m != self._mtime:
            with self.lock:
                try:
                    self.load()
                except (OSError, ValueError):
                    pass                                    # half-written file: keep the old list, try again next time

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---- management
    def create(self, name: str, groups: list[str], minutes_per_day: float = 30.0) -> str:
        code = "-".join(secrets.token_hex(2) for _ in range(3))      # e.g. 3fa9-0c11-b7d2: easy to read out
        with self.lock:
            self.load()
            self.data.setdefault("invites", {})[_h(code)] = {"name": name, "groups": groups, "minutes_per_day": minutes_per_day,
                                                           "created": time.strftime("%Y-%m-%d"), "disabled": False}
            self.save()
        return code

    def list(self) -> list[dict]:
        self.refresh()
        return [{"id": k[:8], **v} for k, v in self.data.get("invites", {}).items()]

    def revoke(self, ident: str) -> int:
        n = 0
        with self.lock:
            self.load()
            for k, v in self.data.get("invites", {}).items():
                if k.startswith(ident) or v["name"] == ident:
                    v["disabled"] = True
                    n += 1
            self.save()
        return n

    # ---- checks at call time
    def verify(self, code: str, peer: str = "") -> tuple[Principal, str, dict]:
        """Returns (principal, invite id, record) or raises PermissionError with a message that is safe to show."""
        self.refresh()
        now = time.time()
        q = self.fail[peer]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= 8:
            raise PermissionError("too many wrong codes, wait a minute")
        k = _h(code or "")
        rec = self.data.get("invites", {}).get(k)
        if not rec or rec.get("disabled"):
            q.append(now)
            raise PermissionError("that invite code is not valid")
        self._roll()
        if self.today[1][k] >= rec.get("minutes_per_day", 30.0):
            raise PermissionError("you have used today's call minutes")
        return Principal(f"invite:{rec['name']}", tuple(rec.get("groups", ()))), k, rec

    def _roll(self) -> None:
        d = time.strftime("%Y-%m-%d")
        if self.today[0] != d:
            self.today = (d, defaultdict(float))

    def begin(self, k: str) -> bool:
        with self.lock:
            if k in self.active:
                return False                      # one call at a time per person
            self.active.add(k)
            return True

    def end(self, k: str, minutes: float) -> None:
        with self.lock:
            self.active.discard(k)
            self._roll()
            self.today[1][k] += minutes
