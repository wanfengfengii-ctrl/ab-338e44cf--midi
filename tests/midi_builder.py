"""Minimal Standard MIDI File builder used by the test-suite."""

from __future__ import annotations


def varlen(value: int) -> bytes:
    if value < 0:
        raise ValueError("delta times are non-negative")
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def chunk(kind: bytes, body: bytes) -> bytes:
    return kind + len(body).to_bytes(4, "big") + body


def header(fmt: int, ntrks: int, division: int) -> bytes:
    return chunk(
        b"MThd",
        fmt.to_bytes(2, "big")
        + ntrks.to_bytes(2, "big")
        + division.to_bytes(2, "big"),
    )


def tempo_event(delta: int, micros_per_quarter: int) -> bytes:
    mpq = micros_per_quarter
    payload = bytes(
        [(mpq >> 16) & 0xFF, (mpq >> 8) & 0xFF, mpq & 0xFF]
    )
    return varlen(delta) + bytes([0xFF, 0x51, 0x03]) + payload


def note(delta: int, channel: int, kind: int, note: int, velocity: int,
         *, running_status: bool = False) -> bytes:
    status = 0x80 | (kind << 4) | channel
    head = varlen(delta)
    if running_status:
        return head + bytes([note, velocity])
    return head + bytes([status, note, velocity])


def program_change(delta: int, channel: int, program: int) -> bytes:
    status = 0xC0 | channel
    return varlen(delta) + bytes([status, program])


def varlen_event(delta: int, raw: bytes) -> bytes:
    return varlen(delta) + raw


def sysex(delta: int, payload: bytes) -> bytes:
    return varlen(delta) + bytes([0xF0]) + varlen(len(payload)) + payload


def end_of_track(delta: int = 0) -> bytes:
    return varlen(delta) + bytes([0xFF, 0x2F, 0x00])


def track(body: bytes) -> bytes:
    return chunk(b"MTrk", body)


def build(fmt: int, division: int, tracks: list[bytes]) -> bytes:
    return header(fmt, len(tracks), division) + b"".join(tracks)
