"""Makes the lab CA and server certificate using plain Python (no OpenSSL needed)."""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import ipaddress
import math
import os
import secrets
import sys
from typing import List, Tuple

# DER encoding


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def INTEGER(n: int) -> bytes:
    return tlv(0x02, n.to_bytes(n.bit_length() // 8 + 1, "big"))


def SEQUENCE(*items: bytes) -> bytes:
    return tlv(0x30, b"".join(items))


def SET(*items: bytes) -> bytes:
    return tlv(0x31, b"".join(items))


def OID(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    body = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(chunk))
    return tlv(0x06, bytes(body))


NULL = b"\x05\x00"
TRUE = b"\x01\x01\xff"


def BIT_STRING(data: bytes, unused: int = 0) -> bytes:
    return tlv(0x03, bytes([unused]) + data)


def OCTET_STRING(data: bytes) -> bytes:
    return tlv(0x04, data)


def UTF8(s: str) -> bytes:
    return tlv(0x0C, s.encode("utf-8"))


def TIME(dt: datetime.datetime) -> bytes:
    if dt.year < 2050:
        return tlv(0x17, dt.strftime("%y%m%d%H%M%SZ").encode())
    return tlv(0x18, dt.strftime("%Y%m%d%H%M%SZ").encode())


def EXPLICIT(n: int, inner: bytes) -> bytes:
    return tlv(0xA0 | n, inner)


def name(common_name: str) -> bytes:
    return SEQUENCE(
        SET(SEQUENCE(OID("2.5.4.10"), UTF8("ZephyQ Lab"))),
        SET(SEQUENCE(OID("2.5.4.3"), UTF8(common_name))),
    )


def extension(oid: str, value: bytes, critical: bool = False) -> bytes:
    return SEQUENCE(OID(oid), *( [TRUE] if critical else [] ), OCTET_STRING(value))


# RSA
_SMALL_PRIMES = [p for p in range(3, 2000) if all(p % q for q in range(2, int(p ** 0.5) + 1))]


def _probable_prime(n: int, rounds: int = 40) -> bool:
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, e: int) -> int:
    while True:
        c = secrets.randbits(bits) | (0b11 << (bits - 2)) | 1   # top 2 bits set -> n has full length
        if math.gcd(c - 1, e) == 1 and _probable_prime(c):
            return c


class RSAKey:
    def __init__(self, bits: int = 2048) -> None:
        self.e = 65537
        while True:
            p, q = _prime(bits // 2, self.e), _prime(bits // 2, self.e)
            if p != q and (p * q).bit_length() == bits:
                break
        self.p, self.q, self.n = p, q, p * q
        self.d = pow(self.e, -1, (p - 1) * (q - 1))

    def public_der(self) -> bytes:
        return SEQUENCE(INTEGER(self.n), INTEGER(self.e))

    def spki(self) -> bytes:
        return SEQUENCE(SEQUENCE(OID("1.2.840.113549.1.1.1"), NULL), BIT_STRING(self.public_der()))

    def key_id(self) -> bytes:
        return hashlib.sha1(self.public_der()).digest()

    def private_pem(self) -> str:
        p, q, d = self.p, self.q, self.d
        der = SEQUENCE(INTEGER(0), INTEGER(self.n), INTEGER(self.e), INTEGER(d), INTEGER(p), INTEGER(q),
                       INTEGER(d % (p - 1)), INTEGER(d % (q - 1)), INTEGER(pow(q, -1, p)))
        return pem("RSA PRIVATE KEY", der)

    def sign_sha256(self, data: bytes) -> bytes:
        digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(data).digest()
        k = (self.n.bit_length() + 7) // 8
        em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")


def pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)


# Certificates
SHA256_RSA = SEQUENCE(OID("1.2.840.113549.1.1.11"), NULL)


def certificate(subject_cn: str, subject_key: RSAKey, issuer_cn: str, issuer_key: RSAKey,
                days: int, extensions: List[bytes]) -> bytes:
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    tbs = SEQUENCE(
        EXPLICIT(0, INTEGER(2)),                            # X.509 v3
        INTEGER(secrets.randbits(63) | 1),                  # serial number
        SHA256_RSA,
        name(issuer_cn),
        SEQUENCE(TIME(now - datetime.timedelta(minutes=5)), TIME(now + datetime.timedelta(days=days))),
        name(subject_cn),
        subject_key.spki(),
        EXPLICIT(3, SEQUENCE(*extensions)),
    )
    return SEQUENCE(tbs, SHA256_RSA, BIT_STRING(issuer_key.sign_sha256(tbs)))


def make_ca(cn: str = "ZephyQ Lab CA", days: int = 3650) -> Tuple[RSAKey, bytes]:
    key = RSAKey()
    exts = [
        extension("2.5.29.19", SEQUENCE(TRUE), critical=True),                    # basicConstraints CA:TRUE
        extension("2.5.29.15", BIT_STRING(b"\x06", unused=1), critical=True),     # keyCertSign, cRLSign
        extension("2.5.29.14", OCTET_STRING(key.key_id())),                       # subjectKeyIdentifier
    ]
    return key, certificate(cn, key, cn, key, days, exts)


def make_server(ca_key: RSAKey, ca_cn: str, ips: List[str], dns: List[str],
                days: int = 825) -> Tuple[RSAKey, bytes]:
    key = RSAKey()
    names = b"".join(tlv(0x82, d.encode("ascii")) for d in dns)                  # dNSName
    names += b"".join(tlv(0x87, ipaddress.ip_address(ip).packed) for ip in ips)  # iPAddress
    exts = [
        extension("2.5.29.19", SEQUENCE(), critical=True),                        # CA:FALSE
        extension("2.5.29.15", BIT_STRING(b"\xa0", unused=5), critical=True),     # digitalSignature, keyEncipherment
        extension("2.5.29.37", SEQUENCE(OID("1.3.6.1.5.5.7.3.1"))),               # extKeyUsage serverAuth
        extension("2.5.29.17", SEQUENCE(names)),                                  # subjectAltName
        extension("2.5.29.14", OCTET_STRING(key.key_id())),
        extension("2.5.29.35", SEQUENCE(tlv(0x80, ca_key.key_id()))),             # authorityKeyIdentifier
    ]
    return key, certificate("ZephyQ server", key, ca_cn, ca_key, days, exts)


def write_all(out: str, ips: List[str], dns: List[str]) -> None:
    os.makedirs(out, exist_ok=True)
    ca_key, ca_der = make_ca()
    srv_key, srv_der = make_server(ca_key, "ZephyQ Lab CA", ips, dns)
    files = {
        "ca.crt": pem("CERTIFICATE", ca_der),
        "ca.key": ca_key.private_pem(),
        "server.crt": pem("CERTIFICATE", srv_der),
        "server.key": srv_key.private_pem(),
    }
    for fname, text in files.items():
        with open(os.path.join(out, fname), "w", encoding="ascii", newline="\n") as fh:
            fh.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", action="append", default=[],
                    help="server IP to put in subjectAltName (repeatable). 127.0.0.1 is always added.")
    ap.add_argument("--dns", action="append", default=[], help="extra DNS name (localhost is always added)")
    ap.add_argument("--out", default="certs", help="output directory (default: %(default)s)")
    args = ap.parse_args(argv)
    ips = list(dict.fromkeys(args.ip + ["127.0.0.1"]))
    dns = list(dict.fromkeys(args.dns + ["localhost"]))
    print("Generating RSA-2048 keys (a few seconds)...")
    write_all(args.out, ips, dns)
    print("Wrote %s/ca.crt, ca.key, server.crt, server.key" % args.out)
    print("Server certificate is valid for: %s" % ", ".join(ips + dns))
    print("Copy ONLY ca.crt to the student machines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
