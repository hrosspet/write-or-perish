"""Import PDFs: text extraction in a child process with limits.

The web workers serve many requests each, cooperatively, so CPU-bound
parsing inside a request would hold up every other request on the same
worker, and a crafted PDF can keep a parser busy or growing for as long
as it likes. The extraction therefore runs in its own Python process
(pdf_extract_worker.py) with a CPU-time limit and an address-space limit
it sets on itself before pypdf is loaded, under a wall-clock timeout
here. The child gets an empty environment (no app secrets) and the
upload as a private temporary file, which is deleted when extract_pdf
returns or raises (a timeout included); the child's stderr is discarded
and nothing it returns is logged except an exception type name.
"""
import json
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime

logger = logging.getLogger(__name__)

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pdf_extract_worker.py")

# --- Limits (heuristics: chosen, lightly measured; listed in the PR) -----
# The child extracts simple text pages at ~80-200 per second (measured on
# a laptop and in the backend's Linux image); dense or oddly built pages
# are several times slower. These keep the whole analyze request (copy
# to disk, start the child, extract, answer) well under a minute.
# CPU seconds the child may use (SIGXCPU, then SIGKILL 2 s later).
PDF_CPU_SECONDS = 20
# Wall-clock seconds before the child is killed (it can also be slow
# while waiting for disk or the CPU).
PDF_WALL_SECONDS = 30
# Address space of the child: the interpreter with pypdf loaded is ~55 MB
# and a 300-page text PDF fits in 64 MB; the rest is for big files (pypdf
# reads the file whole), so a large scanned PDF still gets its own message.
PDF_MEMORY_BYTES = 512 * 1024 * 1024
# Pages and characters of extracted text (checked on the page text and on
# the Markdown made from it). 2M characters is ~1000 dense pages, or 20
# entries of the 100k-character node cap.
PDF_MAX_PAGES = 1000
PDF_MAX_TEXT_CHARS = 2_000_000
# Bytes read from the child's answer at most: 2M characters at up to 4
# bytes each in UTF-8, plus room for JSON escapes and the metadata.
PDF_MAX_OUTPUT_BYTES = 10 * 1024 * 1024

MSG_SCANNED = "This PDF looks scanned: it has no text Loore can read."
MSG_PASSWORD = ("This PDF is password-protected. Remove the password and "
                "upload it again.")
MSG_TOO_SLOW = ("Reading this PDF took too long. Split it into smaller "
                "PDFs and upload them one by one.")
MSG_TOO_BIG = ("This PDF is too large or complex for Loore to read. Split "
               "it into smaller PDFs and upload them one by one.")
MSG_DAMAGED = ("This file is damaged or is not a PDF Loore can read. If it "
               "opens on your device, save it again as a PDF (Print, then "
               "Save as PDF) and upload that copy.")
MSG_TOO_MANY_PAGES = ("This PDF has {pages:,} pages; Loore imports PDFs of "
                      "up to {limit:,} pages. Split it into smaller PDFs and "
                      "upload them one by one.")
MSG_TOO_MUCH_TEXT = ("This PDF has more text than Loore imports at once "
                     "(about {limit:,} characters). Split it into smaller "
                     "PDFs and upload them one by one.")
PDF_UNEXPECTED_ERROR = (
    "Something went wrong while reading this PDF. You can try again, or "
    "save it again as a PDF (Print, then Save as PDF) and upload that copy.")
MSG_PDF_IN_ZIP = ("PDFs inside a zip aren't imported yet. Upload each PDF "
                  "on its own.")
MSG_OLD_DOC = ("Old Word (.doc) files can't be imported. Open it in Word or "
               "Google Docs, save it as .docx, and upload that.")

_TYPE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class PdfImportError(Exception):
    """The PDF cannot be imported; the message is shown to the user."""


def _message(data):
    code = data.get("error")
    if code == "scanned":
        return MSG_SCANNED
    if code in ("password", "encryption_unsupported"):
        return MSG_PASSWORD
    if code == "too_many_pages":
        pages = data.get("pages")
        if not isinstance(pages, int):
            pages = PDF_MAX_PAGES + 1
        return MSG_TOO_MANY_PAGES.format(pages=pages, limit=PDF_MAX_PAGES)
    if code == "too_much_text":
        return MSG_TOO_MUCH_TEXT.format(limit=PDF_MAX_TEXT_CHARS)
    if code == "memory":
        return MSG_TOO_BIG
    if code == "unreadable":
        name = str(data.get("type") or "")
        logger.warning("PDF extraction failed: %s",
                       name if _TYPE_NAME_RE.fullmatch(name) else "unknown")
        return MSG_DAMAGED
    if code == "damaged":
        return MSG_DAMAGED
    # "limits" (the child could not limit itself) or anything unknown:
    # Loore's problem, not the file's.
    raise RuntimeError("PDF worker failed")


def _run_worker(path):
    """Run the child on `path`. Returns (returncode, stdout bytes). Reads
    at most PDF_MAX_OUTPUT_BYTES and waits at most PDF_WALL_SECONDS in
    all; past either, the child is killed and PdfImportError raised."""
    cmd = [sys.executable, "-I", WORKER_PATH, path, str(PDF_CPU_SECONDS),
           str(PDF_MEMORY_BYTES), str(PDF_MAX_PAGES), str(PDF_MAX_TEXT_CHARS)]
    deadline = time.monotonic() + PDF_WALL_SECONDS
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env={})
    chunks, size = [], 0
    try:
        fd = proc.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise PdfImportError(MSG_TOO_SLOW)
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            size += len(chunk)
            if size > PDF_MAX_OUTPUT_BYTES:
                raise PdfImportError(MSG_TOO_BIG)
            chunks.append(chunk)
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise PdfImportError(MSG_TOO_SLOW)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    return proc.returncode, b"".join(chunks)


def parse_date(value):
    """A worker ISO date as a naive datetime, or None."""
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def extract_pdf(fileobj):
    """Extract a PDF upload stream as Markdown.

    Returns {"markdown", "pages", "chapters", "title", "author",
    "created", "modified"} (title/author/dates may be None; dates are
    naive-UTC datetimes). Raises PdfImportError with the user-facing
    message.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        shutil.copyfileobj(fileobj, tmp, 1024 * 1024)
        tmp.flush()
        returncode, stdout = _run_worker(tmp.name)
    if returncode == -signal.SIGXCPU:
        raise PdfImportError(MSG_TOO_SLOW)
    if returncode < 0:
        # SIGKILL after the CPU grace period, or the child died while
        # allocating (SIGSEGV / SIGABRT under the address-space limit).
        raise PdfImportError(MSG_TOO_BIG)
    try:
        data = json.loads(stdout) if returncode == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        logger.warning("PDF extraction failed: exit %s", returncode)
        raise PdfImportError(MSG_DAMAGED)
    if not data.get("ok"):
        raise PdfImportError(_message(data))
    markdown = data.get("markdown") or ""
    if not isinstance(markdown, str) or len(markdown) > PDF_MAX_TEXT_CHARS:
        raise PdfImportError(MSG_TOO_MUCH_TEXT.format(limit=PDF_MAX_TEXT_CHARS))
    return {
        "markdown": markdown,
        "pages": data.get("pages"),
        "chapters": data.get("chapters") or 0,
        "title": data.get("title"),
        "author": data.get("author"),
        "created": parse_date(data.get("created")),
        "modified": parse_date(data.get("modified")),
    }


_JUNK_TITLE_RE = re.compile(
    r"^(?:microsoft (?:word|powerpoint) - |untitled\b)"
    r"|\.(?:docx?|pptx?|pdf|tex|dvi|indd|qxd|rtf|odt)$",
    re.IGNORECASE)


def reference_title(meta_title, filename_without_ext):
    """The title a saved Reference gets: the PDF's own title, unless it is
    empty or an editor's placeholder ("Microsoft Word - draft.docx",
    "untitled"), else the file name."""
    title = (meta_title or "").strip()
    if title and not _JUNK_TITLE_RE.search(title):
        return title
    return filename_without_ext
