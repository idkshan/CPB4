"""Lets you type raw protocol lines (or !long, !bin, !kill, !quit) and see every server reply."""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zephyq import protocol as P                 # noqa: E402
from zephyq.channel import MessageChannel        # noqa: E402
from zephyq.tlsutil import client_context, describe  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--cafile", default=os.path.join("certs", "ca.crt"))
    args = ap.parse_args()

    raw = socket.create_connection((args.host, args.port), timeout=10)
    local = raw.getsockname()
    sock = client_context(args.cafile).wrap_socket(raw, server_hostname=args.host) if args.tls else raw
    print("connected %s:%d -> %s:%d [%s]" % (local[0], local[1], args.host, args.port, describe(sock)))

    def on_message(chan, item):
        if isinstance(item, P.ProtocolError):
            print("<< (unparseable) %s" % item.message)
        else:
            print("<< " + json.dumps(item, ensure_ascii=False))

    def on_close(chan, reason):
        print("** connection closed: %s" % reason)

    chan = MessageChannel(sock, P.MAX_SERVER_LINE, 10**9, on_message, on_close, name="raw")
    chan.start()
    try:
        for line in sys.stdin:
            line = line.rstrip("\r\n").replace("﻿", "")
            if chan.closed.is_set():
                break
            if line.startswith("!long"):
                n = int(line.split()[1]) if len(line.split()) > 1 else P.MAX_CLIENT_LINE + 10
                chan.send(b"A" * n + b"\n")
            elif line == "!bin":
                chan.send(b"\xff\xfe\xfd\n")
            elif line == "!kill":
                linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                sock.close()        # abortive close -> TCP RST
                break
            elif line == "!quit":
                break
            else:
                chan.send(line.encode("utf-8") + b"\n")
        # polite close: QUIT, then let the server's replies and BYE arrive
        chan.send(b'{"type":"QUIT"}\n')
        if not chan.closed.wait(3):
            chan.close()
    except KeyboardInterrupt:
        chan.close()
    chan.closed.wait(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
