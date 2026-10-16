"""Tests for the document-extraction module — offline, fixtures built in-test.

Covers: .txt UTF-8 decode with replacement, .pdf extraction from a minimal
handcrafted PDF (known ``Tj`` text operator), .docx extraction from a
python-docx-generated file, case-insensitive extension dispatch, error paths
(unknown/missing extension, corrupt bytes), extension-only use of ``filename``,
and the exact ``SUPPORTED_EXTENSIONS`` set. No network, no Ollama, no binary
fixture files committed.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import docx
import pytest

import documents
from documents import (
    SUPPORTED_EXTENSIONS,
    DocumentError,
    extract_pages,
    extract_text,
)

# --- Fixture builders (generated in-test, nothing committed) --------------------


def _build_multipage_pdf(texts: list[str]) -> bytes:
    """Build an N-page PDF whose page i shows ``texts[i]`` via ``Tj``."""
    font_number = 3 + 2 * len(texts)
    page_numbers = [3 + 2 * i for i in range(len(texts))]
    stream_numbers = [4 + 2 * i for i in range(len(texts))]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{number} 0 R".encode("ascii") for number in page_numbers)
            + b"] /Count "
            + str(len(texts)).encode("ascii")
            + b" >>"
        ),
        font_number: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(texts):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objects[page_numbers[i]] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 "
            + str(font_number).encode("ascii")
            + b" 0 R >> >> /Contents "
            + str(stream_numbers[i]).encode("ascii")
            + b" 0 R >>"
        )
        objects[stream_numbers[i]] = (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number in range(1, font_number + 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + objects[number] + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {font_number + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {font_number + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _build_minimal_pdf(text: str) -> bytes:
    """Build a one-page PDF whose page stream shows ``text`` via ``Tj``."""
    return _build_multipage_pdf([text])


def _build_docx(*paragraphs: str) -> bytes:
    """Build a .docx byte blob from ``paragraphs`` via python-docx."""
    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- .txt -----------------------------------------------------------------------


def test_txt_decodes_utf8() -> None:
    assert extract_text("héllo wörld".encode(), "note.txt") == "héllo wörld"


def test_txt_invalid_bytes_use_replacement_never_raise() -> None:
    text = extract_text(b"\xff\xfe ok", "note.txt")
    assert "ok" in text
    assert "\ufffd" in text


def test_txt_empty_bytes_yield_empty_string() -> None:
    assert extract_text(b"", "empty.txt") == ""


# --- .md ------------------------------------------------------------------------


def test_md_extracts_raw_text_like_txt() -> None:
    content = b"# Title\n\nSome **body** text."
    assert extract_text(content, "readme.md") == "# Title\n\nSome **body** text."


def test_md_extension_case_insensitive() -> None:
    assert extract_text(b"body", "README.MD") == "body"


def test_md_empty_bytes_yield_empty_string() -> None:
    assert extract_text(b"", "empty.md") == ""


# --- .pdf -----------------------------------------------------------------------


def test_pdf_extracts_known_text() -> None:
    text = extract_text(_build_minimal_pdf("Hello Vektor Document"), "report.pdf")
    assert "Hello" in text
    assert "Vektor" in text
    assert "Document" in text


def test_corrupt_pdf_bytes_raise_document_error() -> None:
    with pytest.raises(DocumentError):
        extract_text(b"this is definitely not a pdf", "broken.pdf")


def test_empty_pdf_bytes_raise_document_error() -> None:
    with pytest.raises(DocumentError):
        extract_text(b"", "broken.pdf")


# --- .docx ----------------------------------------------------------------------


def test_docx_extracts_paragraph_text() -> None:
    text = extract_text(
        _build_docx("First paragraph", "Second paragraph"), "notes.docx"
    )
    assert "First paragraph" in text
    assert "Second paragraph" in text


def test_corrupt_docx_bytes_raise_document_error() -> None:
    with pytest.raises(DocumentError):
        extract_text(b"not a docx zip container", "broken.docx")


def test_empty_docx_bytes_raise_document_error() -> None:
    with pytest.raises(DocumentError):
        extract_text(b"", "broken.docx")


# --- Extension dispatch ----------------------------------------------------------


def test_extensions_case_insensitive() -> None:
    assert extract_text(b"upper txt", "FILE.TXT") == "upper txt"
    pdf_text = extract_text(_build_minimal_pdf("Upper Pdf"), "REPORT.PDF")
    assert "Upper" in pdf_text
    docx_text = extract_text(_build_docx("Upper Docx"), "NOTES.Docx")
    assert "Upper Docx" in docx_text


def test_unknown_extension_raises_document_error() -> None:
    with pytest.raises(DocumentError, match=r"\.exe"):
        extract_text(b"MZ fake binary", "virus.exe")


def test_missing_extension_raises_document_error() -> None:
    with pytest.raises(DocumentError):
        extract_text(b"some text", "README")


def test_empty_filename_raises_document_error() -> None:
    with pytest.raises(DocumentError):
        extract_text(b"some text", "")


def test_filename_used_only_for_dispatch() -> None:
    """Content comes from the bytes; the name only picks the handler."""
    pdf_bytes = _build_minimal_pdf("FromBytes")
    by_one_name = extract_text(pdf_bytes, "a.pdf")
    by_other_name = extract_text(pdf_bytes, "totally-different-name.pdf")
    assert by_one_name == by_other_name
    assert "FromBytes" in by_one_name
    other = extract_text(_build_minimal_pdf("OtherBytes"), "a.pdf")
    assert other != by_one_name


def test_supported_extensions_is_exact_set() -> None:
    assert isinstance(SUPPORTED_EXTENSIONS, frozenset)
    assert SUPPORTED_EXTENSIONS == frozenset({".txt", ".md", ".pdf", ".docx"})


# --- extract_pages ----------------------------------------------------------------


def test_extract_pages_txt_md_docx_are_single_first_page() -> None:
    assert extract_pages(b"hello", "note.txt") == [(1, "hello")]
    assert extract_pages(b"# heading", "note.md") == [(1, "# heading")]
    assert extract_pages(_build_docx("Para"), "notes.docx") == [(1, "Para")]


def test_extract_pages_pdf_one_entry_per_page() -> None:
    pages = extract_pages(_build_multipage_pdf(["Alpha page", "Beta page"]), "book.pdf")
    assert [number for number, _text in pages] == [1, 2]
    assert "Alpha page" in pages[0][1]
    assert "Beta page" in pages[1][1]


def test_extract_pages_pdf_keeps_empty_pages_with_empty_text() -> None:
    """Page numbers stay stable: a text-less page stays as an empty entry."""
    pages = extract_pages(_build_multipage_pdf(["Alpha", "", "Gamma"]), "book.pdf")
    assert [number for number, _text in pages] == [1, 2, 3]
    assert pages[1][1] == ""
    assert "Alpha" in pages[0][1]
    assert "Gamma" in pages[2][1]


def test_extract_text_joins_page_texts_dropping_empty_pages() -> None:
    text = extract_text(_build_multipage_pdf(["Alpha", "", "Gamma"]), "book.pdf")
    assert "Alpha" in text
    assert "Gamma" in text
    assert text.index("Alpha") < text.index("Gamma")


def test_extract_pages_unsupported_extension_raises() -> None:
    with pytest.raises(DocumentError, match=r"\.exe"):
        extract_pages(b"MZ fake binary", "virus.exe")


def test_extract_pages_missing_extension_raises() -> None:
    with pytest.raises(DocumentError):
        extract_pages(b"some text", "README")


def test_extract_pages_corrupt_pdf_raises_document_error() -> None:
    with pytest.raises(DocumentError):
        extract_pages(b"this is definitely not a pdf", "broken.pdf")


def test_module_has_no_telegram_imports() -> None:
    """AST check: documents.py imports no telegram/telebot modules."""
    tree = ast.parse(Path(documents.__file__).read_text(encoding="utf-8"))
    banned = ("telegram", "telebot")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
            assert not any(name.startswith(banned) for name in names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(banned)


def test_unsupported_error_mentions_filename() -> None:
    """A dotfile has an empty suffix — the error still names the file."""
    with pytest.raises(DocumentError) as excinfo:
        extract_text(b"some text", ".txt")
    message = str(excinfo.value)
    assert "Unsupported document type" in message
    assert "'.txt'" in message
