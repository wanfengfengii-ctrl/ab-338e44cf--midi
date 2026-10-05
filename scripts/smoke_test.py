"""End-to-end API smoke test.

POSTs a format-1 multi-track file containing several tempo changes to the
running service and verifies the exact microsecond timeline, then checks
that a malformed payload is rejected with a byte offset.

Exit status is non-zero on the first failed assertion.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from fractions import Fraction

from tests import midi_builder as mb

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
ENDPOINT = f"{API_BASE}/api/midi/normalize"


def post(raw: bytes):
    req = urllib.request.Request(
        ENDPOINT, data=raw, method="POST",
        headers={"Content-Type": "audio/midi"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def build_fixture() -> bytes:
    # Tempo map (absolute ticks): 0 -> 500000, 480 -> 1000000, 960 -> 250000.
    tempo_track = (
        mb.tempo_event(0, 500_000)
        + mb.tempo_event(480, 1_000_000)
        + mb.tempo_event(480, 250_000)
        + mb.end_of_track(0)
    )
    lead = (
        mb.note(0, 0, 0x9, 60, 110)      # tick 0    -> 0 us
        + mb.note(480, 0, 0x9, 64, 110)  # tick 480  -> 500000 us
        + mb.note(480, 0, 0x9, 67, 110)  # tick 960  -> 1500000 us
        + mb.note(240, 0, 0x8, 67, 0)    # tick 1200 -> 1625000 us
        + mb.end_of_track(0)
    )
    bass = (
        mb.note(240, 2, 0x9, 36, 120)    # tick 240  -> 250000 us
        + mb.note(240, 2, 0x9, 40, 120, running_status=True)  # 480
        + mb.note(600, 2, 0x8, 36, 0)    # tick 1080 -> 1562500 us
        + mb.end_of_track(0)
    )
    return mb.build(
        1, 480,
        [mb.track(tempo_track), mb.track(lead), mb.track(bass)],
    )


EXPECTED = {
    (1, 0): Fraction(0),
    (1, 1): Fraction(500_000),
    (1, 2): Fraction(1_500_000),
    (1, 3): Fraction(1_625_000),
    (2, 0): Fraction(250_000),
    (2, 1): Fraction(500_000),
    (2, 2): Fraction(1_562_500),
}


def check(label: bool, message: str) -> None:
    if not label:
        print(f"SMOKE FAIL: {message}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    status, payload = post(build_fixture())
    check(status == 200, f"expected HTTP 200, got {status}: {payload}")
    check(payload["format"] == 1, "format should be 1")
    check(payload["ppqn"] == 480, "ppqn should be 480")
    check(payload["event_count"] == 7, "should list 7 channel events")

    events = payload["events"]
    ordering = [(e["tick"], e["track"], e["index"]) for e in events]
    check(ordering == sorted(ordering), "events must be stably ordered")

    for event in events:
        key = (event["track"], event["index"])
        expected = EXPECTED[key]
        got = Fraction(
            event["time"]["numerator"], event["time"]["denominator"]
        )
        check(
            got == expected,
            f"track {key[0]} index {key[1]} at tick {event['tick']}: "
            f"expected {expected}, got {got}",
        )
        # Fraction must be in lowest terms.
        n, d = event["time"]["numerator"], event["time"]["denominator"]
        check(
            Fraction(n, d).numerator == n and Fraction(n, d).denominator == d,
            f"time {n}/{d} is not expressed in lowest terms",
        )

    # A truncated chunk must be rejected with a locatable offset and must
    # never return a partial timeline.
    bad = mb.header(0, 1, 480) + b"MTrk" + (4).to_bytes(4, "big") + b"\x00\x90"
    status, payload = post(bad)
    check(status == 400, f"malformed file should yield 400, got {status}")
    check(
        isinstance(payload.get("offset"), int),
        "error response must carry a byte offset",
    )
    check("events" not in payload, "errors must not return a partial timeline")

    print("SMOKE OK: multi-track tempo-change normalization verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
