"""MLLP framing.

The Minimum Lower Layer Protocol wraps each message in three bytes: start block ``0x0B``,
payload, end block ``0x1C 0x0D``. That is the whole protocol, and it is where a surprising
share of interface incidents live — a missing end block makes a receiver wait forever or
concatenate two messages into one.

This reader never sees message semantics. It emits frames; parsing is somebody else's job. The
separation matters because the failure modes are different: a framing failure is a transport
incident (the connection is broken, bytes were lost) while a parse failure is a content
incident (the sender's message is wrong), and conflating them sends the wrong team to the
outage.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from cip_interop.domain import InteropError

__all__ = [
    "MllpFramingError",
    "MllpReader",
    "wrap",
]

START_BLOCK = 0x0B
END_BLOCK = 0x1C
CARRIAGE_RETURN = 0x0D


class MllpFramingError(InteropError):
    """A frame boundary was missing or misplaced."""


def wrap(payload: str | bytes, *, encoding: str = "utf-8") -> bytes:
    """Frame a message for transmission.

    UTF-8 and single-byte encodings only. UTF-16 and UTF-32 produce byte values that collide
    with the framing bytes, so a receiver would find a start block in the middle of a name.
    """
    data = payload.encode(encoding) if isinstance(payload, str) else payload
    if bytes([START_BLOCK]) in data or bytes([END_BLOCK]) in data:
        raise MllpFramingError(
            "payload contains an MLLP framing byte (0x0B or 0x1C), which would corrupt the "
            "frame boundary at the receiver"
        )
    return bytes([START_BLOCK]) + data + bytes([END_BLOCK, CARRIAGE_RETURN])


@dataclass(slots=True)
class MllpReader:
    """Accumulates bytes from a stream and yields complete frames.

    Stateful by necessity: TCP delivers arbitrary byte boundaries, so a frame routinely arrives
    across several reads and several frames routinely arrive in one.

    ``max_frame_bytes`` is a required defence, not a tuning knob. Without it a sender that
    opens a connection and never sends an end block grows the buffer until the process dies —
    which is a denial of service reachable by anyone who can open a socket.
    """

    max_frame_bytes: int = 16 * 1024 * 1024
    _buffer: bytearray = field(default_factory=bytearray)
    _in_frame: bool = False
    #: Bytes seen outside any frame. Retained and reported rather than discarded: leading junk
    #: means the stream desynchronised, and silently skipping it hides the incident.
    _discarded: int = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def discarded_bytes(self) -> int:
        return self._discarded

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        """Add bytes and yield every complete frame they finish.

        Yields payloads with the framing bytes removed.
        """
        for byte in chunk:
            if not self._in_frame:
                if byte == START_BLOCK:
                    self._in_frame = True
                    self._buffer.clear()
                else:
                    self._discarded += 1
                continue

            if byte == START_BLOCK:
                # A second start block inside a frame means the previous one was never closed.
                # The partial frame is abandoned rather than merged: concatenating two messages
                # produces one message that looks structurally valid and is clinically wrong.
                dropped = len(self._buffer)
                self._buffer.clear()
                self._discarded += dropped
                continue

            self._buffer.append(byte)

            if len(self._buffer) > self.max_frame_bytes:
                size = len(self._buffer)
                self._buffer.clear()
                self._in_frame = False
                raise MllpFramingError(
                    f"frame exceeded {self.max_frame_bytes} bytes without an end block "
                    f"(buffered {size}); the sender is not framing, or the stream desynchronised"
                )

            if len(self._buffer) >= 2 and self._buffer[-2] == END_BLOCK:
                if self._buffer[-1] != CARRIAGE_RETURN:
                    continue
                payload = bytes(self._buffer[:-2])
                self._buffer.clear()
                self._in_frame = False
                yield payload

    def flush(self) -> bytes:
        """Return and clear any partial frame.

        Called when a connection closes. A partial frame is **not** a message — it is returned
        so it can be logged and investigated, never so it can be parsed.
        """
        partial = bytes(self._buffer)
        self._buffer.clear()
        self._in_frame = False
        return partial
