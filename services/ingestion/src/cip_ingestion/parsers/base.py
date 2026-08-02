"""Parser contract and registry.

Parsers are selected by media type through :class:`ParserRegistry` rather than by an
``if/elif`` chain, so adding a format is a registration rather than an edit to dispatch
logic. Each parser is responsible for one format and returns a
:class:`~cip_ingestion.domain.ParsedDocument`; none of them know about storage, the
database, or tenancy.

Parsers must not raise for *degraded* input — a PDF with one unreadable page, a DOCX with
an unparseable table. They record the problem in ``ParsedDocument.warnings`` and return
what they could extract. Raising :class:`ParserError` is reserved for input that cannot
be parsed at all, because in a clinical corpus a partially-recovered document is far more
useful than a rejected one, provided the degradation is visible to the quality gate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cip_core.errors import CipError
from cip_core.logging import get_logger
from cip_ingestion.domain import ParsedDocument

__all__ = ["DocumentParser", "ParserError", "ParserRegistry"]

_log = get_logger(__name__)


class ParserError(CipError):
    """Raised when a document cannot be parsed at all."""

    status = 422
    problem_type = "parse-failed"
    title = "Document could not be parsed"

    def __init__(self, detail: str, *, media_type: str | None = None) -> None:
        super().__init__(detail, media_type=media_type)
        self.media_type = media_type


@runtime_checkable
class DocumentParser(Protocol):
    """Extracts structured text from raw document bytes."""

    @property
    def name(self) -> str:
        """Stable parser identifier, recorded on the ingestion run for reproducibility."""
        ...

    @property
    def supported_media_types(self) -> frozenset[str]: ...

    def parse(self, data: bytes, *, filename: str | None = None) -> ParsedDocument:
        """Parse ``data``. Raises :class:`ParserError` if the input is unusable."""
        ...


class ParserRegistry:
    """Maps media types to parsers."""

    def __init__(self) -> None:
        self._by_media_type: dict[str, DocumentParser] = {}

    def register(self, parser: DocumentParser) -> None:
        for media_type in parser.supported_media_types:
            existing = self._by_media_type.get(media_type)
            if existing is not None and existing.name != parser.name:
                # Silent replacement would make parser selection depend on import order,
                # which is exactly the kind of bug that only appears in one environment.
                raise ValueError(
                    f"Media type '{media_type}' is already handled by parser "
                    f"'{existing.name}'; refusing to replace it with '{parser.name}'"
                )
            self._by_media_type[media_type] = parser
        _log.debug("parser.registered", parser=parser.name)

    def get(self, media_type: str) -> DocumentParser:
        """Return the parser for ``media_type``.

        Parameters are stripped (``text/plain; charset=utf-8`` → ``text/plain``) because
        browsers and HTTP clients routinely attach them to uploads.
        """
        base_type = media_type.split(";")[0].strip().lower()
        parser = self._by_media_type.get(base_type)
        if parser is None:
            raise ParserError(
                f"No parser registered for media type '{base_type}'", media_type=base_type
            )
        return parser

    def supports(self, media_type: str) -> bool:
        return media_type.split(";")[0].strip().lower() in self._by_media_type

    @property
    def media_types(self) -> frozenset[str]:
        return frozenset(self._by_media_type)
