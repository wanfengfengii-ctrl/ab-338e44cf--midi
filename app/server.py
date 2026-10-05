"""HTTP service exposing MIDI timeline normalization.

Only the Python standard library is required.  The endpoint accepts raw
MIDI bytes and returns the channel events placed on a single microsecond
timeline; structural failures return the offending byte offset.
"""

from __future__ import annotations

import json
import logging
import os
from fractions import Fraction
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .midi import MAX_EVENTS, MidiError, parse_midi

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

logger = logging.getLogger("midi-normalizer")


def _fraction_to_time(value: Fraction) -> dict:
    # Fraction is always stored in lowest terms, so the microsecond time
    # is expressed as the simplest possible exact fraction.
    return {
        "microseconds": f"{value.numerator}/{value.denominator}",
        "numerator": value.numerator,
        "denominator": value.denominator,
    }


def normalize(data: bytes) -> dict:
    midi = parse_midi(data)
    return {
        "format": midi.format,
        "ppqn": midi.ppqn,
        "event_limit": MAX_EVENTS,
        "event_count": len(midi.events),
        "events": [
            {
                "tick": e.tick,
                "track": e.track,
                "index": e.index,
                "channel": e.channel,
                "type": e.subtype,
                "status": f"0x{e.status_nibble:X}",
                "params": e.params,
                "raw": e.raw,
                "time": _fraction_to_time(e.microseconds),
            }
            for e in midi.events
        ],
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "MidiNormalizer/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "use POST /api/midi/normalize"},
        )

    def do_POST(self):
        if self.path != "/api/midi/normalize":
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"}
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(
                HTTPStatus.LENGTH_REQUIRED,
                {"error": "invalid Content-Length header", "offset": 0},
            )
            return
        if length < 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid Content-Length header", "offset": 0},
            )
            return
        if length > MAX_BODY_BYTES:
            # Drain a moderately oversized body so the client can receive
            # the response instead of a connection reset while sending.
            # For absurd declared lengths, close the connection instead.
            if length <= MAX_BODY_BYTES * 2:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "error": (
                        f"MIDI payload exceeds 1 MiB limit "
                        f"({length} bytes)"
                    ),
                    "offset": 0,
                },
            )
            return

        data = self.rfile.read(length) if length else b""
        if len(data) != length:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "truncated request body", "offset": len(data)},
            )
            return
        if not data:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "empty request body", "offset": 0},
            )
            return

        try:
            result = normalize(data)
        except MidiError as exc:
            # Structural errors never produce a partial timeline.
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": exc.message, "offset": exc.offset},
            )
            return

        self._send_json(HTTPStatus.OK, result)

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        logger.info("%s - %s", self.address_string(), fmt % args)


def create_server(host: str = "0.0.0.0", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = create_server(host, port)
    logger.info("MIDI normalizer listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
