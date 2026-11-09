"""Traffic tap: a TCP relay that prints the bytes passing through it.

A Wireshark substitute for showing what is actually on the wire.  Point a
client at the tap instead of the server:

    python tools/tap_proxy.py --listen-port 5051 --target 127.0.0.1:5050
    python -m zephyq.student --host 127.0.0.1 --port 5051 --workstation 11          # plain: JSON is readable
    python -m zephyq.student --host 127.0.0.1 --port 5051 --workstation 11 --tls    # TLS: only ciphertext

Every chunk is shown with its direction and as a hex + ASCII dump, so the
difference between plain TCP and TLS records (which begin with byte 0x16 for
the handshake and 0x17 for application data) is easy to see.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
from typing import Optional


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexpart = " ".join("%02x" % b for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append("  %04x  %-*s  %s" % (i, width * 3, hexpart, text))
    return "\n".join(lines)


class TapProxy:
    def __init__(self, target_host: str, target_port: int, listen_host: str = "127.0.0.1",
                 listen_port: int = 0, verbose: bool = False, max_dump: int = 256) -> None:
        self.target = (target_host, target_port)
        self.verbose = verbose
        self.max_dump = max_dump
        self.client_to_server = bytearray()
        self.server_to_client = bytearray()
        self._lock = threading.Lock()
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.bind((listen_host, listen_port))
        self._lsock.listen(8)
        self.port = self._lsock.getsockname()[1]
        self._stop = False

    def start(self) -> "TapProxy":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def serve_forever(self) -> None:
        self._lsock.settimeout(1.0)
        while not self._stop:
            try:
                client, addr = self._lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                upstream = socket.create_connection(self.target, timeout=10)
            except OSError as exc:
                print("tap: cannot reach %s:%d: %s" % (self.target + (exc,)))
                client.close()
                continue
            label = "%s:%d" % addr
            if self.verbose:
                print("tap: %s connected, relaying to %s:%d" % ((label,) + self.target))
            pair = [client, upstream, 2]     # both sockets close when both directions finish
            threading.Thread(target=self._pump, args=(client, upstream, self.client_to_server,
                                                      "%s -> server" % label, pair), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client, self.server_to_client,
                                                      "server -> %s" % label, pair), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket, record: bytearray, label: str,
              pair: list) -> None:
        src.settimeout(None)
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                with self._lock:
                    record += data
                    if self.verbose:
                        kind = {0x16: " [TLS handshake]", 0x17: " [TLS application data]",
                                0x15: " [TLS alert]"}.get(data[0], "")
                        print("\n%s  %d bytes%s" % (label, len(data), kind))
                        print(hexdump(data[: self.max_dump]) + ("\n  ..." if len(data) > self.max_dump else ""))
                dst.sendall(data)
        except OSError:
            pass
        finally:
            if self.verbose:
                print("tap: %s closed" % label)
            try:
                dst.shutdown(socket.SHUT_WR)      # pass the FIN along
            except OSError:
                pass
            with self._lock:
                pair[2] -= 1
                last = pair[2] == 0
            if last:
                pair[0].close()
                pair[1].close()

    def close(self) -> None:
        self._stop = True
        self._lsock.close()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen-host", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=5051)
    ap.add_argument("--target", default="127.0.0.1:5050", help="server host:port")
    args = ap.parse_args(argv)
    host, _, port = args.target.rpartition(":")
    tap = TapProxy(host, int(port), args.listen_host, args.listen_port, verbose=True)
    print("tap listening on %s:%d -> %s (Ctrl+C to stop)" % (args.listen_host, tap.port, args.target))
    try:
        tap.serve_forever()
    except KeyboardInterrupt:
        pass
    tap.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
