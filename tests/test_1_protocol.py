"""Phase 1 - message format: NDJSON framing, limits and field validation."""
import unittest

import helpers  # noqa: F401  (sets sys.path)
from zephyq import protocol as P


class FramingTests(unittest.TestCase):
    def test_encode_is_one_line_ending_in_lf(self):
        data = P.encode({"type": "JOIN", "problem": "line1\nline2"})
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1, "a newline inside a string must be escaped")
        self.assertEqual(P.decode(data[:-1])["problem"], "line1\nline2")

    def test_message_split_across_reads(self):
        p = P.LineParser(100)
        self.assertEqual(p.feed(b'{"type":"PI'), [])
        self.assertEqual(p.feed(b'NG","id":1}\n'), [{"type": "PING", "id": 1}])

    def test_several_messages_in_one_read(self):
        out = P.LineParser(100).feed(b'{"type":"PING"}\n{"type":"QUIT"}\n{"type":')
        self.assertEqual([m["type"] for m in out], ["PING", "QUIT"])

    def test_crlf_and_blank_lines(self):
        out = P.LineParser(100).feed(b'\r\n\n{"type":"PING"}\r\n')
        self.assertEqual(out, [{"type": "PING"}])

    def test_too_long_without_newline_then_recovers(self):
        p = P.LineParser(32)
        out = p.feed(b"A" * 40)
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], P.FrameTooLong)
        self.assertEqual(p.feed(b"A" * 50), [], "tail of the long line is skipped silently")
        self.assertEqual(p.feed(b'AAA\n{"type":"PING"}\n'), [{"type": "PING"}])

    def test_too_long_with_newline_in_same_read(self):
        out = P.LineParser(16).feed(b"B" * 20 + b'\n{"type":"PING"}\n')
        self.assertIsInstance(out[0], P.FrameTooLong)
        self.assertEqual(out[1], {"type": "PING"})

    def test_exact_limit_is_accepted(self):
        line = b'{"type":"PING"}'
        out = P.LineParser(len(line) + 1).feed(line + b"\n")
        self.assertEqual(out, [{"type": "PING"}])

    def test_bad_frames(self):
        out = P.LineParser(100).feed(b"\xff\xfe\n[1,2]\nhello\n")
        self.assertEqual([e.code for e in out], ["BAD_FRAME"] * 3)

    def test_encode_respects_limit(self):
        with self.assertRaises(ValueError):
            P.encode({"type": "OK", "x": "y" * 100}, limit=50)


class ValidationTests(unittest.TestCase):
    def test_type(self):
        self.assertEqual(P.get_type({"type": "join"}), "JOIN")
        for bad in ({}, {"type": 5}, {"type": "FLY"}):
            with self.assertRaises(P.ProtocolError) as cm:
                P.get_type(bad)
            self.assertEqual(cm.exception.code, "UNKNOWN_TYPE")

    def test_request_id(self):
        self.assertIsNone(P.get_request_id({}))
        self.assertEqual(P.get_request_id({"id": 7}), 7)
        for bad in (-1, True, "7", 2**31):
            with self.assertRaises(P.ProtocolError):
                P.get_request_id({"id": bad})

    def test_int_field(self):
        self.assertEqual(P.require_int({"w": 5}, "w", 1, 9), 5)
        with self.assertRaises(P.ProtocolError) as cm:
            P.require_int({}, "w", 1, 9)
        self.assertEqual(cm.exception.code, "MISSING_FIELD")
        for bad in (0, 10, True, "5", 5.0):
            with self.assertRaises(P.ProtocolError) as cm:
                P.require_int({"w": bad}, "w", 1, 9)
            self.assertEqual(cm.exception.code, "INVALID_FIELD")

    def test_str_field(self):
        self.assertEqual(P.require_str({"n": "  Ana "}, "n", 10), "Ana")
        self.assertEqual(P.require_str({"n": "x" * 10}, "n", 10), "x" * 10)
        for bad in ("", "   ", "x" * 11, "a\tb", "a\x00b", 3):
            with self.assertRaises(P.ProtocolError) as cm:
                P.require_str({"n": bad}, "n", 10)
            self.assertEqual(cm.exception.code, "INVALID_FIELD")

    def test_worst_case_join_fits_client_limit(self):
        # Even if every character is escaped as a \\uXXXX surrogate pair (12 bytes),
        # a maximal JOIN still fits in MAX_CLIENT_LINE.
        import json
        emoji = "\U0001F600"
        msg = {"type": "JOIN", "id": P.MAX_REQUEST_ID, "workstation": P.MAX_WORKSTATION,
               "name": emoji * P.MAX_NAME_LEN, "problem": emoji * P.MAX_PROBLEM_LEN}
        size = len(json.dumps(msg, ensure_ascii=True).encode()) + 1
        self.assertLessEqual(size, P.MAX_CLIENT_LINE)


if __name__ == "__main__":
    unittest.main()
