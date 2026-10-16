"""Document text extraction — .txt/.md/.pdf/.docx bytes → plain text.

Telegram-free: dispatches on the file-name extension (string ops only).
Any failure raises :class:`DocumentError`; library exceptions are wrapped
and logged without the content itself.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import docx
from pypdf import PdfReader

log = logging.getLogger("vektor.documents")


class DocumentError(Exception):
    """Raised for unsupported extensions and any extraction failure."""


def _extract_txt(content: bytes) -> str:
    return content.decode("utf-8", errors="replace")


def _pdf_pages(content: bytes) -> list[tuple[int, str]]:
    reader = PdfReader(io.BytesIO(content))
    return [
        (number, page.extract_text() or "")
        for number, page in enumerate(reader.pages, start=1)
    ]


def _extract_pdf(content: bytes) -> str:
    return "\n".join(text for _, text in _pdf_pages(content) if text)


def _extract_docx(content: bytes) -> str:
    document = docx.Document(io.BytesIO(content))
    return "\n".join(p.text for p in document.paragraphs if p.text)


_EXTRACTORS = {
    ".txt": _extract_txt,
    ".md": _extract_txt,
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
}

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_EXTRACTORS)

_PAGE_FORMATS = frozenset({".pdf"})


def _unsupported_error(filename: str, suffix: str) -> DocumentError:
    return DocumentError(
        f"Unsupported document type: {filename!r} "
        f"(extension {suffix!r}; "
        f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
    )


def extract_pages(content: bytes, filename: str) -> list[tuple[int, str]]:
    """Extract text page by page: PDF yields one entry per page number.

    txt/md/docx yield a single ``(1, text)`` entry; empty PDF pages keep
    page numbers stable; raises :class:`DocumentError` on any failure.
    """
    suffix = Path(filename).suffix.lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise _unsupported_error(filename, suffix)
    try:
        if suffix in _PAGE_FORMATS:
            return _pdf_pages(content)
        return [(1, extractor(content))]
    except Exception as exc:
        log.warning("Text extraction failed for %r: %s", filename, exc, exc_info=True)
        raise DocumentError(f"Failed to extract text from {filename!r}: {exc}") from exc


def extract_text(content: bytes, filename: str) -> str:
    """Extract plain text from ``content``, dispatched by ``filename``'s suffix.

    Thin wrapper over :func:`extract_pages` (page texts joined with
    newlines); raises :class:`DocumentError` under the same conditions.
    """
    return "\n".join(text for _, text in extract_pages(content, filename) if text)
