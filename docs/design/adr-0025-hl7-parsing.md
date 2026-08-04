# ADR-0025: HL7 v2 is scanned with message-declared delimiters, never split on assumed ones

**Status:** Accepted (Phase 6)

## Context

An HL7 v2 message declares its own encoding characters. `MSH-1` is the field separator and
`MSH-2` carries the component, repetition, escape, and subcomponent separators — conventionally
`|` and `^~\&`, but conventionally is not the same as always.

The tempting implementation is `line.split("|")`. It works on every message anyone tests with,
because the conventional delimiters are nearly universal. It fails on the first sending system
that is not conventional, and the failure is silent: every field after the divergence shifts by
one position, so a specimen collection time is read as a result value and a result lands under
the wrong observation identifier. Nothing raises. The message acknowledges `AA`.

Escape sequences have the same shape of failure. `Smith \T\ Sons` is one field containing an
ampersand. Split without decoding and it is a field with two subcomponents, and the second one
is `\ Sons`.

## Decision

Parse with a character scanner that takes its delimiter set from the message being parsed.

- `MSH-1` and `MSH-2` are read positionally from the raw bytes *before* any field splitting,
  because they are the only two fields whose position is knowable without knowing the
  delimiters.
- Escape sequences are decoded on field access, not at tokenise time, so the raw form survives
  for retransmission.
- Repetitions are a list at every level. A field is never collapsed to its first repeat.
- Unknown segments, including all `Z` segments, are retained in order with their raw text.
- MLLP framing (`0x0B` … `0x1C 0x0D`) is handled by a separate reader that never sees message
  semantics, and that refuses a frame missing either boundary rather than guessing where the
  message ended.

A message that cannot be parsed yields `AR` with the failing segment and field named.

## Consequences

- The parser is slower than `split()`. At benchmarked throughput this does not matter; interface
  volumes are thousands per minute, not millions per second.
- Round-tripping is possible: raw retained, delimiters retained, so a message can be forwarded
  byte-identical to how it arrived. Integration engines need this, because the downstream system
  may care about something this platform chose not to model.
- Z-segment content is available but unmapped. Retaining it is not the same as understanding it,
  and the mapping layer must be told what a local segment means before anything reads it.
