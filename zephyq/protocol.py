"""ZephyQ application-layer protocol.

Wire format: newline-delimited JSON (NDJSON) over one TCP (optionally TLS)
connection per client.

* Every message is ONE JSON object encoded in UTF-8, followed by a single
  line feed (0x0A).  The line feed is the message boundary; JSON encoders
  never emit a raw line feed inside a string, so the boundary is unambiguous.
* A client -> server message may be at most MAX_CLIENT_LINE bytes long
  (including the line feed).  A server -> client message may be at most
  MAX_SERVER_LINE bytes long.
* Every message has a "type" field.  A client may add an integer "id"; the
  server copies it into the "re" field of the reply so replies can be matched
  to requests.  Messages the server sends on its own (pushes) never have "re".
* Every client request gets exactly ONE reply: a success reply (OK, QUEUED,
  PONG or BYE) or an ERR reply carrying a machine-readable "code".

See docs/PROTOCOL.md for the full specification.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

PROTOCOL_VERSION = 1
DEFAULT_PORT = 5050
ENCODING = "utf-8"

# ---------------------------------------------------------------- limits ----
MAX_CLIENT_LINE = 4096          # bytes, client -> server, including "\n"
MAX_SERVER_LINE = 262144        # bytes, server -> client, including "\n"
MAX_ACTIVE_TICKETS = 100        # waiting + called tickets at one time
MIN_WORKSTATION = 1
MAX_WORKSTATION = 999
MAX_NAME_LEN = 50               # characters
MAX_PROBLEM_LEN = 200           # characters
MAX_USERNAME_LEN = 32           # characters
MAX_PASSWORD_LEN = 128          # characters
MAX_TOKEN_LEN = 64              # characters
MAX_REQUEST_ID = 2**31 - 1
MAX_LOGIN_ATTEMPTS = 5          # failed LOGINs per connection before disconnect

# ------------------------------------------------------- message types ----
# client -> server
STUDENT_REQUESTS = ("JOIN", "RESUME", "STATUS", "CANCEL")
INSTRUCTOR_REQUESTS = ("LOGIN", "LIST", "NEXT", "DONE", "SUMMARY")
COMMON_REQUESTS = ("PING", "QUIT")
CLIENT_TYPES = STUDENT_REQUESTS + INSTRUCTOR_REQUESTS + COMMON_REQUESTS

# server -> client replies
REPLY_TYPES = ("OK", "QUEUED", "PONG", "BYE", "ERR")
# server -> client pushes (unsolicited)
PUSH_TYPES = ("WELCOME", "POSITION", "CALLED", "RESOLVED", "CANCELLED", "QUEUE", "BYE")

# ---------------------------------------------------------- error codes ----
ERROR_CODES = {
    "BAD_FRAME": "Line is not valid UTF-8 JSON, or is not a JSON object.",
    "TOO_LONG": "Line exceeded the maximum message length and was discarded.",
    "UNKNOWN_TYPE": "The 'type' field is missing or names no known message.",
    "MISSING_FIELD": "A required field is absent.",
    "INVALID_FIELD": "A field has the wrong type, length or range.",
    "WRONG_ROLE": "This connection's role may not send that message.",
    "AUTH_REQUIRED": "Instructor message sent before a successful LOGIN.",
    "AUTH_FAILED": "Unknown username or wrong password.",
    "RATE_LIMITED": "Too many failed logins; the server closes the connection.",
    "DUPLICATE": "That workstation already has an active request.",
    "ALREADY_HAS_TICKET": "This connection already owns an active request.",
    "NO_TICKET": "This connection does not own a request.",
    "NOT_FOUND": "No ticket matches the given number (and token).",
    "INVALID_STATE": "The ticket's status does not allow that operation.",
    "QUEUE_EMPTY": "No student is waiting.",
    "QUEUE_FULL": "The queue reached MAX_ACTIVE_TICKETS.",
    "SERVER_BUSY": "Too many open connections; the server closes the connection.",
    "INTERNAL": "Unexpected server error; the request was not applied.",
}


class ProtocolError(Exception):
    """A message violated the protocol.  Carries an ERROR_CODES key."""

    def __init__(self, code: str, message: str, re: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.re = re


class FrameTooLong(ProtocolError):
    def __init__(self, limit: int) -> None:
        super().__init__("TOO_LONG", "message longer than %d bytes was discarded" % limit)


# ------------------------------------------------------------- encoding ----
def encode(msg: Dict[str, Any], limit: int = MAX_SERVER_LINE) -> bytes:
    """Serialize one message to a single NDJSON line."""
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode(ENCODING) + b"\n"
    if len(data) > limit:
        raise ValueError("encoded message is %d bytes, limit is %d" % (len(data), limit))
    return data


def decode(line: bytes) -> Dict[str, Any]:
    """Parse one line (without the trailing newline) into a message dict."""
    try:
        text = line.decode(ENCODING)
        obj = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("BAD_FRAME", "line is not valid UTF-8 JSON")
    if not isinstance(obj, dict):
        raise ProtocolError("BAD_FRAME", "message must be a JSON object")
    return obj


class LineParser:
    """Incremental splitter for the NDJSON byte stream.

    TCP is a byte stream, so one recv() may hold half a message or several
    messages.  feed() buffers bytes and returns every complete line.  A line
    longer than `limit` is reported once as FrameTooLong and its remaining
    bytes are skipped up to the next newline, so the connection stays usable.
    Empty lines are ignored (handy when testing with a raw terminal client).
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._buf = bytearray()
        self._discarding = False

    def feed(self, data: bytes) -> List[Union[Dict[str, Any], ProtocolError]]:
        self._buf += data
        out: List[Union[Dict[str, Any], ProtocolError]] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                if len(self._buf) >= self.limit:
                    self._buf.clear()
                    if not self._discarding:
                        self._discarding = True
                        out.append(FrameTooLong(self.limit))
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if self._discarding:            # tail of an oversized line
                self._discarding = False
                continue
            if idx + 1 > self.limit:
                out.append(FrameTooLong(self.limit))
                continue
            if line.endswith(b"\r"):        # tolerate CRLF from telnet-style tools
                line = line[:-1]
            if not line.strip():
                continue
            try:
                out.append(decode(line))
            except ProtocolError as exc:
                out.append(exc)
        return out


# ----------------------------------------------------------- validation ----
def get_request_id(msg: Dict[str, Any]) -> Optional[int]:
    rid = msg.get("id")
    if rid is None:
        return None
    if isinstance(rid, bool) or not isinstance(rid, int) or not 0 <= rid <= MAX_REQUEST_ID:
        raise ProtocolError("INVALID_FIELD", "'id' must be an integer from 0 to %d" % MAX_REQUEST_ID)
    return rid


def get_type(msg: Dict[str, Any]) -> str:
    mtype = msg.get("type")
    if not isinstance(mtype, str) or mtype.upper() not in CLIENT_TYPES:
        raise ProtocolError("UNKNOWN_TYPE", "unknown message type %r" % (mtype,))
    return mtype.upper()


def require_int(msg: Dict[str, Any], field: str, lo: int, hi: int) -> int:
    if field not in msg:
        raise ProtocolError("MISSING_FIELD", "'%s' is required" % field)
    val = msg[field]
    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
        raise ProtocolError("INVALID_FIELD", "'%s' must be an integer from %d to %d" % (field, lo, hi))
    return val


def require_str(msg: Dict[str, Any], field: str, max_len: int, strip: bool = True) -> str:
    if field not in msg:
        raise ProtocolError("MISSING_FIELD", "'%s' is required" % field)
    val = msg[field]
    if not isinstance(val, str):
        raise ProtocolError("INVALID_FIELD", "'%s' must be a string" % field)
    if strip:
        val = val.strip()
    if not val:
        raise ProtocolError("INVALID_FIELD", "'%s' must not be empty" % field)
    if len(val) > max_len:
        raise ProtocolError("INVALID_FIELD", "'%s' must be at most %d characters" % (field, max_len))
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in val):
        raise ProtocolError("INVALID_FIELD", "'%s' must not contain control characters" % field)
    return val


def error(code: str, message: str, re: Any = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"type": "ERR", "code": code, "message": message}
    if re is not None:
        msg["re"] = re
    return msg
