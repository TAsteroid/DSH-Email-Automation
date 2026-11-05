"""Plain-text extraction from downloaded attachments (standard library only).

The Agent downloads an attachment with ``attachment.fetch`` and then reads it
with ``attachment.text``; this module turns the file on disk into text:

* text-ish files (``txt`` / ``md`` / ``csv`` / ``json`` / ``ics`` / ``log``)
  are decoded as UTF-8 (BOM tolerated), falling back to GBK;
* ``xlsx`` is read straight out of the ZIP container: shared strings plus
  every worksheet, as TSV (cells joined by tabs, rows by newlines);
* ``docx`` is read out of ``word/document.xml``, one line per paragraph;
* ``pdf`` gets a deliberately small parser (zlib-decompressed FlateDecode
  streams, ``Tj`` / ``TJ`` text operators) for PDFs that still carry a text
  layer; scanned PDFs cannot be handled without OCR.

Anything else, and every malformed/unsupported file, raises
:class:`ExtractError` with a short, content-free message the service maps to
``BAD_REQUEST``.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ElementTree
import zipfile
import zlib
from pathlib import Path

TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".json", ".ics", ".log")
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS + (".xlsx", ".docx", ".pdf")

_TEXT_TYPES = {
    "text/plain": "text",
    "text/markdown": "text",
    "text/csv": "text",
    "application/json": "text",
    "text/calendar": "text",
    "application/calendar+json": "text",
    "text/x-log": "text",
}
_XLSX_TYPES = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
)
_DOCX_TYPES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
)
_PDF_TYPES = ("application/pdf", "application/x-pdf")

# ``(literal) Tj`` / ``[(a) (b)] TJ`` / ``(literal) '`` in a content stream.
_PDF_TEXT_OP = re.compile(
    rb"((?:\((?:[^()\\]|\\.)*\))|(?:\[(?:[^\]\\]|\\.)*\]))\s*(?:Tj|TJ|')",
    re.DOTALL,
)
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_LITERAL = re.compile(rb"\((?:[^()\\]|\\.)*\)")
_PDF_ESCAPES = {
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
    ord("b"): b"\b",
    ord("f"): b"\f",
}


class ExtractError(Exception):
    """Expected extraction failure; the message is safe to show to the user."""


def _local(tag: str) -> str:
    """Element tag without its ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def _content_type_kind(content_type: str) -> str:
    value = (content_type or "").split(";")[0].strip().lower()
    if value in _TEXT_TYPES:
        return "text"
    if value in _XLSX_TYPES:
        return "xlsx"
    if value in _DOCX_TYPES:
        return "docx"
    if value in _PDF_TYPES:
        return "pdf"
    return ""


def classify(filename: str, content_type: str) -> str:
    """Pick the parser from the extension, then from the content type."""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix == ".xlsx":
        return "xlsx"
    if suffix == ".docx":
        return "docx"
    if suffix == ".pdf":
        return "pdf"
    kind = _content_type_kind(content_type)
    if kind:
        return kind
    raise ExtractError(
        "unsupported attachment type"
        + (f": {suffix}" if suffix else "")
        + "; supported extensions: "
        + ", ".join(SUPPORTED_EXTENSIONS)
    )


def extract(path: Path, filename: str, content_type: str) -> tuple[str, str]:
    """Extract text from ``path``; returns ``(kind, text)``."""
    kind = classify(filename, content_type)
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ExtractError(
            f"cannot read attachment file: {type(exc).__name__}"
        ) from None
    try:
        if kind == "text":
            return kind, _read_text(data)
        if kind == "xlsx":
            return kind, _read_xlsx(data)
        if kind == "docx":
            return kind, _read_docx(data)
        if kind == "pdf":
            return kind, _read_pdf(data)
    except ExtractError:
        raise
    except Exception as exc:  # malformed file: never leak a traceback
        raise ExtractError(
            f"{kind} parse failed: {type(exc).__name__}"
        ) from None
    raise ExtractError(
        "unsupported attachment type; supported extensions: "
        + ", ".join(SUPPORTED_EXTENSIONS)
    )


# -- plain text ------------------------------------------------------------


def _read_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractError("cannot decode the attachment as UTF-8 or GBK")


# -- xlsx (zip + SpreadsheetML) --------------------------------------------


def _xml_text(element) -> str:
    """All ``<t>`` text under an element, in document order."""
    return "".join(
        node.text or "" for node in element.iter() if _local(node.tag) == "t"
    )


def _column_index(reference: str, fallback: int) -> int:
    """``"B7"`` -> column 1; a missing reference falls back to sequential."""
    letters = "".join(char for char in reference if char.isalpha()).upper()
    if not letters:
        return fallback
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _row_line(row: object, shared: list[str]) -> str:
    cells: dict[int, str] = {}
    next_column = 0
    for cell in row:
        if _local(cell.tag) != "c":
            continue
        column = _column_index(cell.get("r") or "", next_column)
        next_column = column + 1
        cell_type = (cell.get("t") or "").lower()
        value = ""
        if cell_type == "inlinestr":
            container = next(
                (child for child in cell if _local(child.tag) == "is"), None
            )
            value = _xml_text(container) if container is not None else ""
        else:
            raw = next(
                (child.text or "" for child in cell if _local(child.tag) == "v"),
                "",
            )
            if cell_type == "s":
                try:
                    value = shared[int(raw)]
                except (ValueError, IndexError):
                    value = ""
            elif cell_type == "b":
                value = "TRUE" if raw.strip() == "1" else "FALSE" if raw.strip() == "0" else raw
            else:
                value = raw
        cells[column] = value
    if not cells:
        return ""
    width = max(cells) + 1
    return "\t".join(cells.get(index, "") for index in range(width))


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        payload = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(payload)
    return [
        _xml_text(node) for node in root.iter() if _local(node.tag) == "si"
    ]


def _worksheet_names(archive: zipfile.ZipFile) -> list[str]:
    names = [
        name
        for name in archive.namelist()
        if name.startswith("xl/worksheets/") and name.endswith(".xml")
    ]

    def order(name: str) -> int:
        match = re.search(r"sheet(\d+)\.xml$", name)
        return int(match.group(1)) if match else 0

    return sorted(names, key=order)


def _read_xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared = _shared_strings(archive)
        sheets = _worksheet_names(archive)
        if not sheets:
            raise ExtractError("xlsx has no worksheet")
        lines: list[str] = []
        for name in sheets:
            root = ElementTree.fromstring(archive.read(name))
            for row in root.iter():
                if _local(row.tag) == "row":
                    lines.append(_row_line(row, shared))
    return "\n".join(lines).rstrip("\n")



# -- docx (zip + WordprocessingML) -----------------------------------------


def _read_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        try:
            payload = archive.read("word/document.xml")
        except KeyError:
            raise ExtractError("docx has no document.xml") from None
    root = ElementTree.fromstring(payload)
    paragraphs: list[str] = []
    for paragraph in root.iter():
        if _local(paragraph.tag) != "p":
            continue
        parts: list[str] = []
        for node in paragraph.iter():
            tag = _local(node.tag)
            if tag == "t":
                parts.append(node.text or "")
            elif tag == "tab":
                parts.append("\t")
            elif tag in ("br", "cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs).rstrip("\n")


# -- pdf (zlib streams + text operators) -----------------------------------


def _pdf_unescape(literal: bytes) -> bytes:
    body = literal[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        byte = body[index]
        if byte != 0x5C or index + 1 >= len(body):
            out.append(byte)
            index += 1
            continue
        following = body[index + 1]
        if following in _PDF_ESCAPES:
            out.extend(_PDF_ESCAPES[following])
            index += 2
        elif 0x30 <= following <= 0x37:  # octal escape, up to 3 digits
            digits = bytearray([following])
            index += 2
            while index < len(body) and len(digits) < 3 and 0x30 <= body[index] <= 0x37:
                digits.append(body[index])
                index += 1
            out.append(int(digits.decode("ascii"), 8) & 0xFF)
        elif following in (0x0A, 0x0D):  # escaped line continuation
            index += 2
        else:
            out.append(following)
            index += 2
    return bytes(out)


def _pdf_decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _pdf_streams(data: bytes) -> list[bytes]:
    streams: list[bytes] = []
    for match in _PDF_STREAM.finditer(data):
        raw = match.group(1)
        header = data[max(0, match.start() - 400) : match.start()]
        if b"/FlateDecode" in header or b"FlateDecode" in header:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                try:
                    raw = zlib.decompressobj().decompress(raw)
                except zlib.error:
                    continue
        streams.append(raw)
    return streams


def _read_pdf(data: bytes) -> str:
    lines: list[str] = []
    for content in _pdf_streams(data):
        for match in _PDF_TEXT_OP.finditer(content):
            operand = match.group(1)
            if operand.startswith(b"["):
                fragments = _PDF_LITERAL.findall(operand)
                text = "".join(_pdf_decode(_pdf_unescape(item)) for item in fragments)
            else:
                text = _pdf_decode(_pdf_unescape(operand))
            lines.append(text)
    text = "\n".join(line for line in lines if line)
    if not text.strip():
        raise ExtractError("PDF 无可提取文本（可能是扫描件）")
    return text
