"""Small helpers shared by the student and instructor command-line programs."""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import threading
from typing import Optional, Sequence

from . import protocol as P
from . import ui
from .tlsutil import client_context

_print_lock = threading.Lock()
_prompt = "> "


def add_connection_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--host", default="127.0.0.1",
                    help="server IP address, e.g. 192.168.56.1 in the lab (default: %(default)s)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT, help="server TCP port (default: %(default)s)")
    ap.add_argument("--tls", action="store_true", help="connect with TLS (the server must use --tls)")
    ap.add_argument("--cafile", default=os.path.join("certs", "ca.crt"),
                    help="CA certificate used to verify the server (default: %(default)s)")
    add_color_arg(ap)


def add_color_arg(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                    help="colored output (default: auto = only in a real terminal)")


def ssl_context_from_args(args):
    if not args.tls:
        return None
    return client_context(args.cafile)


def set_prompt(label: str) -> None:
    """The prompt shows who you are, e.g. 'ws 11 › ' or 'instructor › '."""
    global _prompt
    _prompt = ui.style(label, "bold", "cyan") + " " + ui.symbol("arrow") + " "


def prompt() -> str:
    return _prompt


def notify(text: str) -> None:
    """Print a message that arrived while the user may be typing at the prompt."""
    with _print_lock:
        sys.stdout.write("\r\n" + text + "\n" + _prompt)
        sys.stdout.flush()


def say(text: str = "") -> None:
    with _print_lock:
        print(text, flush=True)


def ask(prompt_text: str) -> str:
    try:
        return input(prompt_text).replace("﻿", "")     # drop a byte-order mark from piped input
    except EOFError:
        return "quit"


def header(title: str, host: str, port: int, tls_info: str, extra: Sequence[Sequence[str]] = ()) -> str:
    """The box shown right after connecting."""
    encrypted = not tls_info.startswith("plain")
    security = ui.ok("[%s] encrypted" % tls_info) if encrypted else ui.warn("plain TCP (NOT encrypted)")
    rows = [("Server", ui.bold("%s:%d" % (host, port))), ("Connection", security)] + list(extra)
    return ui.panel("ZephyQ " + ui.symbol("dot") + " " + title, ui.fields(rows))


def command_help(commands: Sequence[Sequence[str]]) -> str:
    return "\n" + ui.table(["Command", "Short", "What it does"],
                           [(ui.info(c), s, d) for c, s, d in commands], flex=2) + "\n"


def state_line(text: str) -> str:
    """Color a connection-state message by what it says."""
    low = text.lower()
    if low.startswith("reconnected"):
        return ui.tag("connection", "green") + " " + ui.ok(text)
    if "disconnected" in low or "failed" in low or "could not" in low:
        return ui.tag("connection", "red") + " " + ui.bad(text)
    return ui.tag("connection", "yellow") + " " + ui.warn(text)


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
