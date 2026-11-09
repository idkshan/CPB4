"""Small helpers shared by the student and instructor command-line programs."""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import threading
from typing import Optional

from . import protocol as P
from .tlsutil import client_context

_print_lock = threading.Lock()
PROMPT = "> "


def add_connection_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--host", default="127.0.0.1",
                    help="server IP address, e.g. 192.168.56.1 in the lab (default: %(default)s)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT, help="server TCP port (default: %(default)s)")
    ap.add_argument("--tls", action="store_true", help="connect with TLS (the server must use --tls)")
    ap.add_argument("--cafile", default=os.path.join("certs", "ca.crt"),
                    help="CA certificate used to verify the server (default: %(default)s)")


def ssl_context_from_args(args):
    if not args.tls:
        return None
    return client_context(args.cafile)


def notify(text: str) -> None:
    """Print a message that arrived while the user may be typing at the prompt."""
    with _print_lock:
        sys.stdout.write("\r\n" + text + "\n" + PROMPT)
        sys.stdout.flush()


def say(text: str = "") -> None:
    with _print_lock:
        print(text, flush=True)


def ask(prompt: str) -> str:
    try:
        return input(prompt).replace("﻿", "")     # drop a byte-order mark from piped input
    except EOFError:
        return "quit"


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def seconds_since(iso_time: Optional[str]) -> Optional[float]:
    if not iso_time:
        return None
    then = datetime.datetime.fromisoformat(iso_time)
    return (datetime.datetime.now(then.tzinfo) - then).total_seconds()


def clock_time(iso_time: Optional[str]) -> str:
    if not iso_time:
        return "-"
    return datetime.datetime.fromisoformat(iso_time).strftime("%H:%M:%S")
