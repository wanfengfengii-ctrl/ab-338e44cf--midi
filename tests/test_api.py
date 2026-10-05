"""Tests for the HTTP layer (request size, error payloads, JSON shape)."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import MAX_BODY_BYTES, create_server

from . import midi_builder as mb
from .test_midi_parser import complex_multi_track_file


class _ServerFixture:
    def __init__(self):
        self.server: ThreadingHTTPServer = create_server(
            "127.0.0.1", 0
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def post(data: bytes, port: int):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/midi/normalize",
        data=data,
        method="POST",
        headers={"Content-Type": "audio/midi"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class ApiTests(unittest.TestCase):
    def test_health(self):
        with _ServerFixture() as fx:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{fx.port}/health", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read())["status"], "ok")

    def test_success_payload_shape(self):
        with _ServerFixture() as fx:
            status, payload = post(complex_multi_track_file(), fx.port)
        self.assertEqual(status, 200)
        self.assertEqual(payload["format"], 1)
        self.assertEqual(payload["ppqn"], 480)
        self.assertEqual(payload["event_count"], 7)
        self.assertEqual(len(payload["events"]), 7)
        keys = [
            "tick", "track", "index", "channel", "type", "status",
            "params", "raw", "time",
        ]
        for event in payload["events"]:
            self.assertEqual(set(event), set(keys))
            self.assertTrue(event["time"]["microseconds"])
            self.assertIsInstance(event["time"]["numerator"], int)
            self.assertIsInstance(event["time"]["denominator"], int)
        ordered = [
            (e["tick"], e["track"], e["index"]) for e in payload["events"]
        ]
        self.assertEqual(ordered, sorted(ordered))
        # Earliest event of the complex file.
        self.assertEqual(
            payload["events"][0]["time"]["microseconds"], "0/1"
        )
        self.assertEqual(payload["events"][0]["time"]["denominator"], 1)

    def test_structural_error_carries_offset(self):
        bad = mb.header(0, 1, 480) + b"MTrk" + (3).to_bytes(4, "big") + b"\x00\x90"
        with _ServerFixture() as fx:
            status, payload = post(bad, fx.port)
        self.assertEqual(status, 400)
        self.assertIn("offset", payload)
        self.assertIsInstance(payload["offset"], int)
        self.assertNotIn("events", payload)

    def test_empty_body(self):
        with _ServerFixture() as fx:
            status, payload = post(b"", fx.port)
        self.assertEqual(status, 400)
        self.assertIn("offset", payload)

    def test_body_over_limit(self):
        big = b"\x00" * (MAX_BODY_BYTES + 1)
        with _ServerFixture() as fx:
            status, payload = post(big, fx.port)
        self.assertEqual(status, 413)
        self.assertIn("1 MiB", payload["error"])

    def test_wrong_endpoint(self):
        with _ServerFixture() as fx:
            req = urllib.request.Request(
                f"http://127.0.0.1:{fx.port}/nope", data=b"x", method="POST"
            )
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
