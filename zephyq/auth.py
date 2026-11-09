"""Instructor credentials: salted PBKDF2-SHA256 hashes in a JSON file.

Passwords are never stored in plain text.  Manage accounts with:

    python -m zephyq.auth add <username>
    python -m zephyq.auth remove <username>
    python -m zephyq.auth list
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
from typing import Dict

DEFAULT_PATH = os.path.join("config", "instructors.json")
ITERATIONS = 200_000


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def make_record(password: str) -> Dict[str, object]:
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "iterations": ITERATIONS,
            "hash": _derive(password, salt, ITERATIONS).hex()}


class CredentialStore:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
        self.users: Dict[str, Dict[str, object]] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                self.users = json.load(fh)
        # Used for unknown usernames so a wrong username costs the same time
        # as a wrong password (no username probing by timing).
        self._dummy = make_record(secrets.token_hex(8))

    def verify(self, username: str, password: str) -> bool:
        rec = self.users.get(username)
        known = rec is not None
        rec = rec or self._dummy
        digest = _derive(password, bytes.fromhex(str(rec["salt"])), int(rec["iterations"]))  # type: ignore[arg-type]
        return hmac.compare_digest(digest, bytes.fromhex(str(rec["hash"]))) and known

    def set_password(self, username: str, password: str) -> None:
        self.users[username] = make_record(password)
        self.save()

    def remove(self, username: str) -> bool:
        if self.users.pop(username, None) is None:
            return False
        self.save()
        return True

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.users, fh, indent=2)
        os.replace(tmp, self.path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zephyq.auth", description="Manage ZephyQ instructor accounts.")
    ap.add_argument("--file", default=DEFAULT_PATH, help="credential file (default: %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="add an instructor or change a password")
    p_add.add_argument("username")
    p_rm = sub.add_parser("remove", help="remove an instructor")
    p_rm.add_argument("username")
    sub.add_parser("list", help="list instructor usernames")
    args = ap.parse_args(argv)

    store = CredentialStore(args.file)
    if args.cmd == "list":
        print("\n".join(sorted(store.users)) or "(no instructors)")
    elif args.cmd == "remove":
        print("removed" if store.remove(args.username) else "no such user")
    else:
        name = args.username.strip()
        if not name or len(name) > 32:
            print("username must be 1-32 characters", file=sys.stderr)
            return 2
        pw = getpass.getpass("New password for %s: " % name)
        if len(pw) < 8 or len(pw) > 128:
            print("password must be 8-128 characters", file=sys.stderr)
            return 2
        if getpass.getpass("Repeat password: ") != pw:
            print("passwords do not match", file=sys.stderr)
            return 2
        store.set_password(name, pw)
        print("saved %s to %s" % (name, args.file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
