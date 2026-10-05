"""Unit tests for the strict SMF parser and timeline conversion."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.midi import (
    DEFAULT_MICROS_PER_QUARTER,
    MAX_EVENTS,
    MidiError,
    parse_midi,
)

from . import midi_builder as mb


def make_format0(events: bytes, division: int = 480, *,
                 with_end: bool = True) -> bytes:
    body = events
    if with_end:
        body += mb.end_of_track()
    return mb.build(0, division, [mb.track(body)])


def make_format1(tracks: list[bytes], division: int = 480) -> bytes:
    return mb.build(1, division, [mb.track(t) for t in tracks])


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_default_tempo_constant_ppqn():
    # Two notes at ticks 0 and 240 with 480 PPQN, default 500000 us/quarter.
    data = make_format0(
        mb.note(0, 0, 0x9, 60, 100)
        + mb.note(240, 0, 0x8, 60, 0)
    )
    midi = parse_midi(data)
    assert midi.format == 0
    assert midi.ppqn == 480
    times = [e.microseconds for e in midi.events]
    assert times[0] == Fraction(0)
    assert times[1] == Fraction(250_000)
    assert midi.events[0].track == 0
    assert midi.events[0].index == 0
    assert midi.events[1].index == 1


def test_running_status():
    body = (
        mb.note(0, 0, 0x9, 60, 100)
        + mb.note(0, 0, 0x9, 64, 100, running_status=True)
        + mb.program_change(10, 0, 5)
        # Running status is still the program change, so a different
        # command must restate its status byte.
        + mb.note(0, 0, 0x9, 67, 90)
        + mb.note(0, 0, 0x9, 71, 90, running_status=True)
        + mb.end_of_track()
    )
    data = mb.build(0, 480, [mb.track(body)])
    midi = parse_midi(data)
    assert len(midi.events) == 5
    assert [e.subtype for e in midi.events] == [
        "note_on", "note_on", "program_change", "note_on", "note_on"
    ]
    assert midi.events[3].params["note"] == 67
    assert midi.events[4].params["note"] == 71
    # Program change is one data byte.
    assert midi.events[2].raw == bytes([0xC0, 0x05]).hex()


def test_tempo_change_takes_effect_at_its_tick():
    # tick 0 tempo default; tempo 250000 at tick 480; events at 480 and 720.
    body = (
        mb.tempo_event(0, DEFAULT_MICROS_PER_QUARTER)
        + mb.note(0, 0, 0x9, 60, 100)  # t=0
        + mb.tempo_event(480, 250_000)
        + mb.note(0, 0, 0x9, 62, 100)  # tick 480, boundary
        + mb.note(240, 0, 0x9, 64, 100)  # tick 720, new tempo region
        + mb.end_of_track()
    )
    data = mb.build(0, 480, [mb.track(body)])
    midi = parse_midi(data)
    times = [e.microseconds for e in midi.events]
    assert times[0] == Fraction(0)
    # 480 ticks at 500000/480 = 500000 us
    assert times[1] == Fraction(500_000)
    # plus 240 ticks at 250000/480 = 125000 us
    assert times[2] == Fraction(625_000)


def test_fractional_microseconds_in_lowest_terms():
    # 96 PPQN: one tick = 500000/96 = 15625/3 us.
    body = mb.note(1, 0, 0x9, 60, 100) + mb.end_of_track()
    data = mb.build(0, 96, [mb.track(body)])
    midi = parse_midi(data)
    value = midi.events[0].microseconds
    assert value == Fraction(15625, 3)
    assert value.numerator == 15625
    assert value.denominator == 3


def test_format1_global_tempo_only_from_first_track():
    tempo_track = (
        mb.tempo_event(0, 500_000)
        + mb.tempo_event(480, 200_000)
        + mb.end_of_track(0)
    )
    track_a = (
        mb.note(0, 0, 0x9, 60, 100)  # tick 0
        + mb.note(480, 0, 0x9, 62, 100)  # tick 480 -> 500000 us
        + mb.end_of_track(0)
    )
    track_b = (
        mb.note(240, 1, 0x9, 64, 100)  # tick 240 -> 250000 us
        + mb.note(480, 1, 0x9, 65, 100)  # tick 720: 600000 us
        + mb.end_of_track(0)
    )
    data = make_format1([tempo_track, track_a, track_b])
    midi = parse_midi(data)
    by_key = {(e.track, e.index): e.microseconds for e in midi.events}
    assert by_key[(1, 0)] == Fraction(0)
    assert by_key[(1, 1)] == Fraction(500_000)
    assert by_key[(2, 0)] == Fraction(250_000)
    assert by_key[(2, 1)] == Fraction(600_000)
    # Stable ordering: tick, track, index.
    ordered = [(e.tick, e.track, e.index) for e in midi.events]
    assert ordered == sorted(ordered)
    assert ordered[0] == (0, 1, 0)


def test_pitch_bend_is_14bit():
    body = (
        mb.varlen_event(0, bytes([0xE0, 0x00, 0x40]))
        + mb.end_of_track()
    )
    data = mb.build(0, 480, [mb.track(body)])
    midi = parse_midi(data)
    assert midi.events[0].params["value"] == 0x2000  # 8192


def test_sysex_is_accepted_but_not_a_channel_event():
    body = (
        mb.sysex(0, bytes([0x7E, 0x7F, 0x09, 0x01, 0xF7]))
        + mb.note(0, 0, 0x9, 60, 100)
        + mb.end_of_track()
    )
    data = mb.build(0, 480, [mb.track(body)])
    midi = parse_midi(data)
    assert len(midi.events) == 1
    # Sysex cancels running status.
    assert midi.events[0].subtype == "note_on"


# ---------------------------------------------------------------------------
# Structural errors
# ---------------------------------------------------------------------------


def test_empty_input_rejected():
    with pytest.raises(MidiError) as exc:
        parse_midi(b"")
    assert exc.value.offset == 0


def test_missing_header():
    data = mb.chunk(b"XXXX", b"")
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "MThd" in exc.value.message
    assert exc.value.offset == 0


def test_bad_header_length():
    data = mb.chunk(b"MThd", b"\x00" * 5)
    with pytest.raises(MidiError):
        parse_midi(data)


def test_unsupported_format_2():
    data = mb.header(2, 1, 480) + mb.track(mb.end_of_track())
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "format" in exc.value.message


def test_zero_ppqn_rejected():
    data = mb.header(0, 1, 0) + mb.track(mb.end_of_track())
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "PPQN" in exc.value.message


def test_smpte_division_rejected():
    data = mb.header(0, 1, 0xE250) + mb.track(mb.end_of_track())
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "SMPTE" in exc.value.message


def test_track_count_mismatch_rejected():
    data = mb.header(1, 2, 480) + mb.track(mb.end_of_track())
    with pytest.raises(MidiError):
        parse_midi(data)


def test_format0_multiple_tracks_rejected():
    data = (
        mb.header(0, 2, 480)
        + mb.track(mb.end_of_track())
        + mb.track(mb.end_of_track())
    )
    with pytest.raises(MidiError):
        parse_midi(data)


def test_unknown_chunk_type_rejected():
    data = (
        mb.header(0, 1, 480)
        + mb.chunk(b"XFoo", b"")
        + mb.track(mb.end_of_track())
    )
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "chunk" in exc.value.message


def test_chunk_length_beyond_eof_rejected():
    data = mb.header(0, 1, 480) + b"MTrk" + (100).to_bytes(4, "big")
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "exceeds file size" in exc.value.message


def test_undeclared_trailing_bytes_rejected():
    # Header declares a 0-length MTrk but extra bytes follow it.
    data = mb.header(0, 1, 480) + mb.chunk(b"MTrk", b"") + b"\x00\x00"
    with pytest.raises(MidiError):
        parse_midi(data)


def test_truncated_track_body_rejected():
    data = (
        mb.header(0, 1, 480)
        + b"MTrk"
        + (2).to_bytes(4, "big")
        + bytes([0x00, 0x90])  # delta+status, data and EOT missing
    )
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "truncated" in exc.value.message
    assert isinstance(exc.value.offset, int)


def test_running_status_without_previous_status_rejected():
    body = bytes([0x00, 0x3C, 0x64]) + mb.end_of_track()
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "running status" in exc.value.message


def test_illegal_status_byte_rejected():
    body = bytes([0x00, 0xF3, 0x01]) + mb.end_of_track()
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "illegal status" in exc.value.message


def test_running_status_cancelled_by_meta_then_data_byte_rejected():
    body = (
        mb.note(0, 0, 0x9, 60, 100)
        + mb.end_of_track(0)
        + bytes([0x00, 0x3C, 0x64])  # no EOT allowed after anyway
    )
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError):
        parse_midi(data)


def test_event_after_end_of_track_rejected():
    body = mb.end_of_track() + mb.note(0, 0, 0x9, 60, 100)
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "End Of Track" in exc.value.message


def test_missing_end_of_track_rejected():
    data = make_format0(mb.note(0, 0, 0x9, 60, 100), with_end=False)
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "End Of Track" in exc.value.message


def test_bad_tempo_length_rejected():
    body = (
        mb.varlen(0) + bytes([0xFF, 0x51, 0x02, 0x00, 0x00])
        + mb.end_of_track()
    )
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "Tempo" in exc.value.message


def test_varlen_five_bytes_rejected():
    body = bytes([0x80, 0x80, 0x80, 0x80, 0x00]) + mb.end_of_track()
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "variable-length" in exc.value.message


def test_varlen_truncated_rejected():
    body = bytes([0x80])
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError):
        parse_midi(data)


def test_format1_tempo_in_second_track_rejected():
    tempo_track = mb.end_of_track()
    bad_track = mb.tempo_event(0, 400_000) + mb.end_of_track()
    data = make_format1([tempo_track, bad_track])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert "tempo" in exc.value.message
    # Offset points inside the second track chunk in the whole file.
    assert exc.value.offset > 14


def test_too_many_events_rejected():
    body = b"".join(
        mb.program_change(0, 0, i % 128) for i in range(MAX_EVENTS + 1)
    ) + mb.end_of_track()
    data = mb.build(0, 480, [mb.track(body)])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert str(MAX_EVENTS) in exc.value.message


def test_tracks_plus_events_share_the_limit():
    # Two tracks plus 10000 channel events exceeds the shared budget of
    # 10000 tracks+events.
    tempo_track = mb.end_of_track()
    body = b"".join(
        mb.program_change(0, 0, i % 128) for i in range(MAX_EVENTS)
    ) + mb.end_of_track()
    data = mb.build(1, 480, [mb.track(tempo_track), mb.track(body)])
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    assert str(MAX_EVENTS) in exc.value.message


def test_error_offset_points_into_file():
    # Truncate right in the middle of a two-byte data payload; the offset
    # must be an absolute file position, not a track-relative one.
    body = bytes([0x00, 0x90, 0x3C])  # missing velocity + EOT
    track_chunk = mb.chunk(b"MTrk", body)
    data = mb.header(0, 1, 480) + track_chunk
    with pytest.raises(MidiError) as exc:
        parse_midi(data)
    # Offset is the absolute position where the velocity was expected.
    assert exc.value.offset == len(data)


# ---------------------------------------------------------------------------
# Multi-tempo multi-track fixture reused by the API smoke test
# ---------------------------------------------------------------------------


def complex_multi_track_file() -> bytes:
    tempo_track = (
        mb.tempo_event(0, 500_000)
        + mb.tempo_event(480, 1_000_000)
        + mb.tempo_event(480, 250_000)  # absolute tick 960
        + mb.end_of_track(0)
    )
    lead = (
        mb.note(0, 0, 0x9, 60, 110)
        + mb.note(480, 0, 0x9, 64, 110)   # 500000 us
        + mb.note(480, 0, 0x9, 67, 110)   # tick 960: +1e6 = 1500000 us
        + mb.note(240, 0, 0x8, 67, 0)     # tick 1200: +125000 = 1625000
        + mb.end_of_track(0)
    )
    bass = (
        mb.note(240, 2, 0x9, 36, 120)    # tick 240: 250000 us
        + mb.note(240, 2, 0x9, 40, 120, running_status=True)  # tick 480
        + mb.note(600, 2, 0x8, 36, 0)    # tick 1080: 1500000 +
        #   120 ticks * 250000/480 = 62500 -> 1562500
        + mb.end_of_track(0)
    )
    return make_format1([tempo_track, lead, bass])


def test_complex_multi_track_expected_times():
    data = complex_multi_track_file()
    midi = parse_midi(data)
    expected = {
        (1, 0): Fraction(0),
        (1, 1): Fraction(500_000),
        (1, 2): Fraction(1_500_000),
        (1, 3): Fraction(1_625_000),
        (2, 0): Fraction(250_000),
        (2, 1): Fraction(500_000),
        (2, 2): Fraction(1_562_500),
    }
    actual = {(e.track, e.index): e.microseconds for e in midi.events}
    assert actual == expected
