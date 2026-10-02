"""Document text extraction — .txt/.pdf/.docx bytes → plain text.

Telegram-free helper for the bot's document-upload flow: given raw file
bytes and the original file name, :func:`extract_text` dispatches on the
file-name extension (string ops only — no filesystem access, no network,
no env reads) and returns the extracted plain text. Any failure —
unsupported extension, corrupt container — raises :class:`DocumentError`;
library exceptions are wrapped and logged with ``exc_info``, never leaked
and never logged with the content itself.
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


def _extract_pdf(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    texts = (page.extract_text() for page in reader.pages)
    return "\n".join(text for text in texts if text)


def _extract_docx(content: bytes) -> str:
    document = docx.Document(io.BytesIO(content))
    return "\n".join(p.text for p in document.paragraphs if p.text)


_EXTRACTORS = {
    ".txt": _extract_txt,
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
}

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_EXTRACTORS)


def extract_text(content: bytes, filename: str) -> str:
    """Extract plain text from ``content``, dispatched by ``filename``'s suffix.

    ``filename`` is used only for extension dispatch — the content always
    comes from ``content``. Raises :class:`DocumentError` for unsupported
    or missing extensions and for any extraction failure (the original
    exception is preserved via ``from``).
    """
    suffix = Path(filename).suffix.lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise DocumentError(
            f"Unsupported document type: {filename!r} "
            f"(extension {suffix!r}; "
            f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
        )
    try:
        return extractor(content)
    except Exception as exc:
        log.warning("Text extraction failed for %r: %s", filename, exc, exc_info=True)
        raise DocumentError(f"Failed to extract text from {filename!r}: {exc}") from exc
