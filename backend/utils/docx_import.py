"""Convert Word (.docx) documents to Markdown for import.

Pipeline: mammoth (docx -> HTML) -> markdownify (HTML -> Markdown).
mammoth's own Markdown writer is deliberately not used: it is deprecated,
escapes every "." and "-", and writes <a id> tags.

A .docx is a zip of XML parts, so the file is checked before conversion
(see the limits below). Word never writes a DOCTYPE, so any XML part
that has one is rejected.
"""
import re
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree

import mammoth
from bs4 import BeautifulSoup
from markdownify import markdownify

# --- Limits (heuristics: chosen, not measured; listed in the PR) ---------
# Combined uncompressed size of the XML parts the converter parses, summed
# over every .docx in one upload request. The converter needs ~55 MB of
# memory per MB of XML, so 5 MB is about 300 MB peak, roughly a
# 300-page document.
MAX_PARSED_XML_BYTES = 5 * 1024 * 1024
# Total uncompressed size of all parts (images included).
MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
# Overall uncompressed:compressed ratio. Word XML compresses ~10-20x and
# images barely at all. Only checked above MIN_BYTES_FOR_RATIO_CHECK.
MAX_COMPRESSION_RATIO = 200
MIN_BYTES_FOR_RATIO_CHECK = 5 * 1024 * 1024
# Number of zip entries (a real document has a few dozen).
MAX_ENTRIES = 5000
# A .docx taken out of a zip is spooled to a temp file; this many bytes
# stay in memory before it spills to disk.
SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024


class DocxImportError(Exception):
    """The file cannot be imported; the message is shown to the user."""


def check_extension(filename):
    """Raise for a .docm; return True for .docx, False for anything else."""
    lower = filename.lower()
    if lower.endswith(".docm"):
        raise DocxImportError(
            "macro-enabled Word files (.docm) are not accepted; "
            "save it as .docx and try again")
    return lower.endswith(".docx")


class ParseBudget:
    """Bytes of parsed XML still allowed in one upload request."""

    def __init__(self, limit=None):
        self.remaining = MAX_PARSED_XML_BYTES if limit is None else limit


def _parsed_part_names(zf):
    """Names of the zip entries the converter will parse, resolved with
    the converter's own functions (package relationships, then the main
    document's relationships). Each relationships part is checked before
    the converter reads it. Raises DocxImportError on anything that
    cannot be resolved."""
    from mammoth import docx as mdocx
    from mammoth.zips import open_zip

    names = set()
    names_in_zip = set(zf.namelist())

    def check(name):
        if name in names_in_zip:
            names.add(name)
            _check_no_doctype(zf, name)

    check("[Content_Types].xml")
    check("_rels/.rels")
    mz = open_zip(zf.fp, "r")
    try:
        main = mdocx._find_document_filename(
            mz, mdocx._read_relationships(mz, "_rels/.rels"))
    except Exception:
        raise DocxImportError("this is not a Word document")
    rels = mdocx._find_relationships_path_for(main)
    check(rels)
    try:
        paths = mdocx._find_part_paths(mz)
    except Exception:
        raise DocxImportError("this is not a Word document")
    for part in (paths.main_document, paths.styles, paths.numbering,
                 paths.footnotes, paths.endnotes, paths.comments):
        check(part)
        check(mdocx._find_relationships_path_for(part))
    check("docProps/core.xml")  # read for the document date
    if main not in names:
        raise DocxImportError("this is not a Word document")
    return names


def _check_no_doctype(zf, name):
    """Reject an XML part with a DOCTYPE (Word never writes one). The size
    is bounded by the budget check that runs before any part is read."""
    info = zf.getinfo(name)
    if info.file_size > MAX_PARSED_XML_BYTES:
        raise DocxImportError("the document text is too large to import")
    with zf.open(info) as part:
        data = part.read(info.file_size + 1)
    # UTF-16 parts have NUL bytes between characters
    if b"<!doctype" in data.lower().replace(b"\x00", b""):
        raise DocxImportError("the document contains unsupported markup")


def _validate(zf, budget):
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise DocxImportError("the document has too many parts")
    total = sum(i.file_size for i in infos)
    compressed = sum(i.compress_size for i in infos)
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise DocxImportError("the document is too large when unpacked")
    if (total > MIN_BYTES_FOR_RATIO_CHECK
            and total > MAX_COMPRESSION_RATIO * max(compressed, 1)):
        raise DocxImportError("the document is compressed abnormally")
    # Size first (declared sizes only, nothing read), then content checks.
    # The relationships parts are read to resolve the rest, so they are
    # size-limited before that.
    for name in ("[Content_Types].xml", "_rels/.rels"):
        try:
            if zf.getinfo(name).file_size > MAX_PARSED_XML_BYTES:
                raise DocxImportError("the document text is too large to import")
        except KeyError:
            pass
    names = _parsed_part_names(zf)
    size = sum(zf.getinfo(n).file_size for n in names)
    if size > budget.remaining:
        raise DocxImportError(
            "too much text to import in one go (limit "
            f"{MAX_PARSED_XML_BYTES // (1024 * 1024)} MB of document XML "
            "per upload); upload fewer or smaller documents at a time")
    budget.remaining -= size


def modified_date(zf):
    """dcterms:modified from docProps/core.xml as a naive UTC datetime,
    or None when missing or unreadable."""
    try:
        root = ElementTree.fromstring(zf.read("docProps/core.xml"))
    except (KeyError, ElementTree.ParseError):
        return None
    for el in root.iter("{http://purl.org/dc/terms/}modified"):
        raw = (el.text or "").strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _image_handler(image):
    # No image bytes are read: only the alt text is kept.
    return {"src": "", "alt": image.alt_text or ""}


def _clean_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        img.replace_with(f"[image: {alt}]" if alt else "[image]")
    # Empty anchors (bookmarks) and in-document links (footnote
    # back-references) carry no content.
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if not href and not a.get_text().strip():
            a.decompose()
        elif href.startswith(("#footnote-ref", "#endnote-ref")):
            a.decompose()
        elif href.startswith("#") or not href:
            a.unwrap()
    # Word tables rarely mark a header row; Markdown tables need one.
    for table in soup.find_all("table"):
        first = table.find("tr")
        if first and not first.find("th"):
            for td in first.find_all("td", recursive=False):
                td.name = "th"
    return str(soup)


class _Seekable:
    """Python 3.9's SpooledTemporaryFile (also what uploads arrive as)
    has no seekable(), which zipfile needs."""

    def __init__(self, f):
        self._f = f
        self.read, self.seek, self.tell = f.read, f.seek, f.tell

    def seekable(self):
        return True


def convert_docx(fileobj, budget=None):
    """Convert a seekable binary .docx stream.

    Returns (markdown, modified_datetime_or_None). Raises DocxImportError.
    """
    if not hasattr(fileobj, "seekable"):
        fileobj = _Seekable(fileobj)
    try:
        with zipfile.ZipFile(fileobj) as zf:
            _validate(zf, budget or ParseBudget())
            modified = modified_date(zf)
    except zipfile.BadZipFile:
        raise DocxImportError("this is not a valid Word document")
    try:
        fileobj.seek(0)
        result = mammoth.convert_to_html(
            fileobj,
            convert_image=mammoth.images.img_element(_image_handler))
        md = markdownify(_clean_html(result.value), heading_style="ATX",
                         bullets="-")
    except Exception as e:
        raise DocxImportError(
            f"the document could not be read ({type(e).__name__})")
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md, modified


_HEADING_RE = re.compile(r"^(#{1,6}) \S", re.M)


def split_chapters(markdown):
    """Split converted Markdown at the highest heading level that appears
    more than once. Text before the first such heading is its own part.
    Returns [markdown] when no heading level repeats."""
    matches = [(len(m.group(1)), m.start())
               for m in _HEADING_RE.finditer(markdown)]
    counts = {}
    for lvl, _ in matches:
        counts[lvl] = counts.get(lvl, 0) + 1
    repeated = [lvl for lvl, n in counts.items() if n > 1]
    if not repeated:
        return [markdown]
    level = min(repeated)
    starts = [pos for lvl, pos in matches if lvl == level]
    cuts = ([0] if starts[0] != 0 else []) + starts + [len(markdown)]
    parts = [markdown[a:b].strip() for a, b in zip(cuts, cuts[1:])]
    return [p for p in parts if p]
