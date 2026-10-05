"""Strict binary parser for Standard MIDI Files (SMF).

Supported subset:

* format 0 and format 1;
* positive PPQN (division) timing; SMPTE/frames-per-second division rejected;
* at most 10 000 track/channel events in total.

Every malformed input raises :class:`MidiError` carrying the byte offset at
which the problem was detected, so callers can locate the failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

DEFAULT_MICROS_PER_QUARTER = 500_000
MAX_EVENTS = 10_000

META = 0xFF
SYSEX_START = 0xF0
SYSEX_ESCAPE = 0xF7


class MidiError(ValueError):
    """A structural problem located at a specific byte offset."""

    def __init__(self, message: str, offset: int):
        super().__init__(f"{message} at byte offset {offset}")
        self.message = message
        self.offset = offset


@dataclass(frozen=True)
class ChannelEvent:
    """A channel voice event positioned on the unified timeline."""

    tick: int
    track: int
    index: int
    microseconds: Fraction
    status_nibble: int
    channel: int
    subtype: str
    params: dict
    raw: bytes


@dataclass(frozen=True)
class TempoPoint:
    tick: int
    micros_per_quarter: int


# ---------------------------------------------------------------------------
# Low level cursor
# ---------------------------------------------------------------------------


class _Cursor:
    """Bounded reader over the whole file.

    Reported offsets are always absolute positions inside the original
    byte string, including when the cursor is restricted to a chunk body.
    """

    def __init__(self, data: bytes, start: int = 0, end: Optional[int] = None):
        self.data = data
        self.pos = start
        self.end = len(data) if end is None else end

    def require(self, n: int, what: str) -> bytes:
        if self.pos + n > self.end:
            raise MidiError(
                f"truncated data while reading {what}", self.pos
            )
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u8(self, what: str) -> int:
        return self.require(1, what)[0]

    def u16(self, what: str) -> int:
        return int.from_bytes(self.require(2, what), "big")

    def u32(self, what: str) -> int:
        return int.from_bytes(self.require(4, what), "big")

    def varlen(self, what: str) -> int:
        """Read a MIDI variable-length quantity (max four payload bytes)."""
        start = self.pos
        value = 0
        for i in range(4):
            byte = self.u8(f"{what} variable-length quantity")
            value = (value << 7) | (byte & 0x7F)
            if byte < 0x80:
                return value
        # Fifth byte would still carry a continuation bit: illegal in SMF.
        raise MidiError(
            "variable-length quantity exceeds four bytes", start
        )


# ---------------------------------------------------------------------------
# Chunk scanning
# ---------------------------------------------------------------------------


def _read_chunks(data: bytes):
    cur = _Cursor(data)
    chunks = []
    while cur.pos < cur.end:
        chunk_start = cur.pos
        chunk_type = cur.require(4, "chunk type")
        length = cur.u32("chunk length")
        body_start = cur.pos
        if body_start + length > cur.end:
            raise MidiError(
                f"chunk {chunk_type!r} length {length} exceeds file size",
                body_start,
            )
        chunks.append((bytes(chunk_type), body_start, length, chunk_start))
        cur.pos = body_start + length
    return chunks


# ---------------------------------------------------------------------------
# Track event parsing
# ---------------------------------------------------------------------------

_CHANNEL_SUBTYPES = {
    0x8: ("note_off", ("note", "velocity")),
    0x9: ("note_on", ("note", "velocity")),
    0xA: ("polyphonic_aftertouch", ("note", "pressure")),
    0xB: ("control_change", ("controller", "value")),
    0xC: ("program_change", ("program",)),
    0xD: ("channel_aftertouch", ("pressure",)),
    0xE: ("pitch_bend", ("value",)),
}

_PARAM_LENGTHS = {0x8: 2, 0x9: 2, 0xA: 2, 0xB: 2, 0xC: 1, 0xD: 1, 0xE: 2}


@dataclass
class _RawTrack:
    events: list  # list[tuple[tick, subtype, nibble, channel, params, raw]]
    tempos: list  # list[tuple[tick, micros_per_quarter, byte_offset]]
    count: int


def _parse_track(
    data: bytes, body_start: int, length: int, track_index: int
) -> _RawTrack:
    cur = _Cursor(data, body_start, body_start + length)
    events: list = []
    tempos: list = []
    count = 0
    tick = 0
    running_status: Optional[int] = None
    ended = False

    while cur.pos < cur.end:
        event_start = cur.pos
        if ended:
            raise MidiError(
                "event found after End Of Track meta event",
                event_start,
            )
        delta = cur.varlen("delta-time")
        tick += delta

        status_byte = cur.u8("status byte")
        if status_byte < 0x80:
            # Running status: the byte is the first data byte and the
            # previously seen channel status remains in force.
            if running_status is None:
                raise MidiError(
                    "running status without a preceding channel status byte",
                    event_start,
                )
            status = running_status
            first_data = status_byte
            data_bytes = bytearray()
        else:
            status = status_byte
            first_data = None
            data_bytes = bytearray()

        high = status & 0xF0

        if 0x80 <= high <= 0xE0:
            nibble = high >> 4
            channel = status & 0x0F
            length = _PARAM_LENGTHS[nibble]
            if first_data is not None:
                data_bytes.append(first_data)
            while len(data_bytes) < length:
                data_bytes.append(cur.u8("channel event data byte"))
            name, param_names = _CHANNEL_SUBTYPES[nibble]
            if nibble == 0xE:
                params = {
                    param_names[0]: data_bytes[0]
                    | (data_bytes[1] << 7)
                }
            else:
                params = dict(zip(param_names, data_bytes))
            raw = bytes([status]) + bytes(data_bytes)
            events.append(
                (tick, name, nibble, channel, params, raw)
            )
            count += 1
            running_status = status

        elif status == META:
            meta_type = cur.u8("meta event type")
            length = cur.varlen("meta event length")
            payload_start = cur.pos
            payload = cur.require(length, "meta event payload")
            if meta_type == 0x51:
                if length != 3:
                    raise MidiError(
                        "Set Tempo meta event must be exactly three bytes",
                        payload_start,
                    )
                micros = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                tempos.append((tick, micros, payload_start))
            elif meta_type == 0x2F:
                if length != 0:
                    raise MidiError(
                        "End Of Track meta event must have zero length",
                        payload_start,
                    )
                ended = True
            running_status = None

        elif status in (SYSEX_START, SYSEX_ESCAPE):
            length = cur.varlen("sysex event length")
            payload_start = cur.pos
            cur.require(length, "sysex event payload")
            running_status = None

        else:
            # 0xF1..0xF6, 0xF8..0xFE (including MIDI time code / song
            # position) are system common/real-time bytes and have no
            # defined representation in an SMF track chunk.
            raise MidiError(
                f"illegal status byte 0x{status:02X} in track",
                event_start,
            )

        if count > MAX_EVENTS:
            raise MidiError(
                f"total channel events exceed limit of {MAX_EVENTS}",
                event_start,
            )

    if not ended:
        raise MidiError(
            "track chunk is missing End Of Track meta event",
            body_start + length,
        )

    return _RawTrack(events=events, tempos=tempos, count=count)


# ---------------------------------------------------------------------------
# File assembly and timeline conversion
# ---------------------------------------------------------------------------


@dataclass
class MidiFile:
    format: int
    ppqn: int
    events: list  # list[ChannelEvent]


def _build_tempo_map(
    tracks: list[_RawTrack], fmt: int
) -> list[TempoPoint]:
    if fmt == 0:
        tempo_events = [
            (tick, micros) for tick, micros, _ in tracks[0].tempos
        ]
    else:
        tempo_events = [
            (tick, micros) for tick, micros, _ in tracks[0].tempos
        ]
        for track in tracks[1:]:
            if track.tempos:
                _, _, offset = track.tempos[0]
                raise MidiError(
                    "format 1 tempo events are only permitted in the "
                    "first (tempo) track",
                    offset,
                )
    tempo_events.sort(key=lambda item: item[0])
    return [TempoPoint(tick=t, micros_per_quarter=m) for t, m in tempo_events]


def _tick_to_micros(tick: int, tempos: list[TempoPoint], ppqn: int) -> Fraction:
    """Convert an absolute tick to microseconds.

    Tempo at tick ``t`` applies to every interval starting at ``t`` until
    the next tempo tick.  Time is kept as an exact :class:`Fraction`.
    """
    micros = Fraction(0)
    boundary_tick = 0
    mspq = DEFAULT_MICROS_PER_QUARTER
    for point in tempos:
        if point.tick > tick:
            break
        if point.tick > boundary_tick:
            micros += Fraction(
                (point.tick - boundary_tick) * mspq, ppqn
            )
            boundary_tick = point.tick
        mspq = point.micros_per_quarter
    micros += Fraction((tick - boundary_tick) * mspq, ppqn)
    return micros


def parse_midi(data: bytes) -> MidiFile:
    if not isinstance(data, (bytes, bytearray)):
        raise MidiError("input must be raw bytes", 0)
    data = bytes(data)

    chunks = _read_chunks(data)
    if not chunks:
        raise MidiError("empty file: no chunks found", 0)

    header = chunks[0]
    if header[0] != b"MThd":
        raise MidiError("missing MThd header chunk", header[3])
    if header[2] != 6:
        raise MidiError(
            f"MThd chunk length must be 6, got {header[2]}", header[1]
        )

    hbody = data[header[1] : header[1] + 6]
    fmt = int.from_bytes(hbody[0:2], "big")
    ntrks = int.from_bytes(hbody[2:4], "big")
    division = int.from_bytes(hbody[4:6], "big")

    if fmt not in (0, 1):
        raise MidiError(
            f"unsupported MIDI format {fmt}; only 0 and 1 are supported",
            header[1],
        )
    if division == 0:
        raise MidiError("division (PPQN) must be a positive number", header[1] + 4)
    if division & 0x8000:
        raise MidiError(
            "SMPTE/timecode division is not supported; positive PPQN required",
            header[1] + 4,
        )

    track_chunks = [c for c in chunks[1:] if c[0] == b"MTrk"]
    extra = [c for c in chunks[1:] if c[0] != b"MTrk"]
    if extra:
        etype, _, _, estart = extra[0]
        raise MidiError(
            f"unexpected chunk type {etype!r}; only MTrk chunks may follow MThd",
            estart,
        )
    if len(track_chunks) != ntrks:
        raise MidiError(
            f"header declares {ntrks} track(s) but {len(track_chunks)} MTrk "
            "chunk(s) are present",
            header[1] + 2,
        )
    if ntrks == 0:
        raise MidiError("file declares zero tracks", header[1] + 2)
    if ntrks > MAX_EVENTS:
        raise MidiError(
            f"track count exceeds limit of {MAX_EVENTS}", header[1] + 2
        )
    if fmt == 0 and ntrks != 1:
        raise MidiError(
            "format 0 files must contain exactly one track", header[1] + 2
        )

    tracks: list[_RawTrack] = []
    total = ntrks  # tracks count toward the 10 000 item budget
    for idx, (_, body_start, length, _) in enumerate(track_chunks):
        track = _parse_track(data, body_start, length, idx)
        total += track.count
        if total > MAX_EVENTS:
            raise MidiError(
                "total of tracks and channel events exceeds limit of "
                f"{MAX_EVENTS}",
                body_start,
            )
        tracks.append(track)

    tempos = _build_tempo_map(tracks, fmt)

    events: list[ChannelEvent] = []
    for idx, track in enumerate(tracks):
        for in_track_index, (tick, subtype, nibble, channel, params, raw) in enumerate(
            track.events
        ):
            micros = _tick_to_micros(tick, tempos, division)
            events.append(
                ChannelEvent(
                    tick=tick,
                    track=idx,
                    index=in_track_index,
                    microseconds=micros,
                    status_nibble=nibble,
                    channel=channel,
                    subtype=subtype,
                    params=params,
                    raw=raw.hex(),
                )
            )

    events.sort(key=lambda e: (e.tick, e.track, e.index))
    return MidiFile(format=fmt, ppqn=division, events=events)
