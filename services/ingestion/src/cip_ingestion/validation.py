"""Upload validation and content identity.

Runs before anything is stored, because every later stage is more expensive and because
storing unvalidated bytes means storing whatever a caller sent.

The part worth reading closely is :func:`sniff_media_type`. A caller's declared
``Content-Type`` is an assertion, not a fact — it is wrong routinely by accident (browsers
guess from extensions) and occasionally on purpose. Dispatching a parser on an unverified
declaration means handing arbitrary bytes to a parser that assumes a format, so the
declared type is checked against the payload's magic bytes and a mismatch is rejected.

Content identity is a SHA-256 over the raw bytes. It serves three purposes at once:
duplicate detection, the storage address (``build_object_key``), and an integrity check
for anything later re-read from storage.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from cip_core.config import IngestionSettings
from cip_core.errors import (
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)

__all__ = [
    "MEDIA_TYPE_EXTENSIONS",
    "ValidatedUpload",
    "compute_content_hash",
    "sanitize_filename",
    "sniff_media_type",
    "validate_upload",
]

#: Magic-byte signatures, longest-prefix first so a more specific match wins.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (
        b"PK\x03\x04",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
)

MEDIA_TYPE_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}

#: Filenames are metadata, never paths. Anything that could be interpreted as a path
#: separator or traversal is stripped rather than escaped.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._\- ]+")
_MAX_FILENAME_LENGTH = 255


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """A payload that passed validation, with its derived identity."""

    data: bytes
    media_type: str
    """The *verified* media type. Where the declaration and the payload disagree and the
    payload is authoritative (a real PDF declared as ``application/octet-stream``), this
    is the sniffed type."""
    filename: str | None
    size_bytes: int
    content_hash: str
    extension: str


def compute_content_hash(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sanitize_filename(filename: str | None) -> str | None:
    """Reduce a client-supplied filename to a safe metadata string.

    Takes only the final path component (defeating both POSIX and Windows traversal),
    strips characters outside a conservative allowlist, and truncates. Returns ``None``
    if nothing usable survives, which is preferable to inventing a placeholder name.
    """
    if not filename:
        return None
    candidate = filename.replace("\\", "/").split("/")[-1]
    candidate = unicodedata.normalize("NFC", candidate)
    candidate = _UNSAFE_FILENAME_CHARS.sub("_", candidate).strip(" .")
    if not candidate:
        return None
    return candidate[:_MAX_FILENAME_LENGTH]


def sniff_media_type(data: bytes) -> str | None:
    """Infer the media type from magic bytes, or ``None`` if unrecognised.

    Plain text has no signature, so it is inferred rather than matched: a payload that
    decodes as UTF-8 and contains no NUL bytes is treated as text. The NUL check is what
    separates text from binary formats that happen to decode without raising.
    """
    for signature, media_type in _MAGIC_SIGNATURES:
        if data.startswith(signature):
            return media_type

    sample = data[:8192]
    if b"\x00" in sample:
        return None
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte sequence split by the sample boundary is not evidence of binary.
        try:
            data[:8192].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    return "text/plain"


def validate_upload(
    data: bytes,
    *,
    declared_media_type: str,
    filename: str | None,
    settings: IngestionSettings,
) -> ValidatedUpload:
    """Validate an upload and derive its identity.

    Raises :class:`~cip_core.errors.ValidationFailedError`,
    :class:`~cip_core.errors.PayloadTooLargeError`, or
    :class:`~cip_core.errors.UnsupportedMediaTypeError`.
    """
    if not data:
        raise ValidationFailedError("Uploaded payload is empty")

    size = len(data)
    if size > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"Payload is {size} bytes; the limit is {settings.max_upload_bytes} bytes"
        )

    declared = (declared_media_type or "").split(";")[0].strip().lower()
    sniffed = sniff_media_type(data)

    resolved = _resolve_media_type(declared=declared, sniffed=sniffed)
    if resolved not in settings.allowed_media_types:
        raise UnsupportedMediaTypeError(
            f"Media type '{resolved}' is not accepted; "
            f"allowed: {', '.join(sorted(settings.allowed_media_types))}"
        )

    return ValidatedUpload(
        data=data,
        media_type=resolved,
        filename=sanitize_filename(filename),
        size_bytes=size,
        content_hash=compute_content_hash(data),
        extension=MEDIA_TYPE_EXTENSIONS.get(resolved, ""),
    )


def _resolve_media_type(*, declared: str, sniffed: str | None) -> str:
    """Reconcile the declared and sniffed media types.

    Rules, in order:

    * Unrecognisable payload → reject. Nothing downstream can parse it, and guessing
      would mean handing unknown bytes to a parser that assumes a format.
    * No declaration, or a generic one (``application/octet-stream``) → trust the payload.
      Generic declarations carry no information and are what most non-browser clients send.
    * Declaration and payload agree → use it.
    * They disagree → reject. Silently overriding the caller hides a real problem: either
      the client is mislabelling a whole class of documents, or the payload is not what
      the caller thinks it is. Both warrant a visible failure, not a quiet correction.
    """
    if sniffed is None:
        raise UnsupportedMediaTypeError(
            "Payload does not match any supported format (expected PDF, DOCX, or text)"
        )
    if not declared or declared == "application/octet-stream":
        return sniffed
    if declared == sniffed:
        return declared

    # A DOCX is a ZIP container, so any OOXML declaration over a ZIP payload is consistent.
    if sniffed.endswith("wordprocessingml.document") and declared.startswith(
        "application/vnd.openxmlformats"
    ):
        return sniffed

    raise ValidationFailedError(
        f"Declared media type '{declared}' does not match the payload content "
        f"(detected '{sniffed}')"
    )
