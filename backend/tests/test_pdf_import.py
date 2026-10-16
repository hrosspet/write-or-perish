"""PDF import: /api/import/analyze + /api/import/confirm, the extraction
child process and its clean-up heuristics, and the old-.doc message.

PDFs are built in the tests with pypdf's writer (a Helvetica text layer
per page), so no fixture files are needed. Encryption is ON (KMS faked)
to check that imported text, own entries and a saved Reference alike,
is encrypted at rest.
"""

import io
import os
import sys
import zipfile
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402
from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import (  # noqa: E402
    DecodedStreamObject, DictionaryObject, NameObject,
)

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import ExternalItem, Node, User  # noqa: E402
import backend.models as _real_backend_models    # noqa: E402
from backend.utils import encryption, pdf_import  # noqa: E402
from backend.utils import pdf_extract_worker as worker  # noqa: E402


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.import_data import import_bp
    app.register_blueprint(import_bp, url_prefix="/api")
    return app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    monkeypatch.setattr(encryption, "_wrap_dek", lambda dek: b"wrapped:" + dek)
    monkeypatch.setattr(encryption, "_unwrap_dek",
                        lambda w: w[len(b"wrapped:"):])
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def client(app):
    u = User(username="alice", approved=True, plan="alpha")
    _db.session.add(u)
    _db.session.flush()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    return c


# --- PDF fixtures ------------------------------------------------------------

def _pdf(pages, outline=None, meta=None, password=None):
    """PDF bytes with one text line per list item on each page.

    outline: [(title, page_index, [(child_title, page_index), ...])].
    password: encrypt with this user password ("" = opens without one).
    """
    w = PdfWriter()
    font = w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for lines in pages:
        page = w.add_blank_page(width=612, height=792)
        ops = ["BT", "/F1 10 Tf", "13 TL", "60 750 Td"]
        for line in lines:
            esc = (line.replace("\\", "\\\\").replace("(", "\\(")
                   .replace(")", "\\)"))
            ops.append(f"({esc}) Tj T*")
        ops.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1"))
        page[NameObject("/Contents")] = w._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
    for title, page_index, children in outline or []:
        parent = w.add_outline_item(title, page_index)
        for child_title, child_page in children or []:
            w.add_outline_item(child_title, child_page, parent=parent)
    if meta:
        w.add_metadata(meta)
    if password is not None:
        w.encrypt(user_password=password, owner_password="owner-pw",
                  algorithm="AES-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# A full body line (~70 characters) and a short line that ends a paragraph.
FULL = "the quick brown fox jumps over the lazy dog and keeps on running far"


def _body(word, n=4):
    return [f"{word} {FULL}"] * n + [f"{word} ends here."]


META = {"/Title": "A Study of Things", "/Author": "Jane Doe",
        "/CreationDate": "D:20200102030405Z",
        "/ModDate": "D:20210304050607Z"}


def _paper():
    pages = [
        ["A Study of Things", "Introduction"] + _body("intro"),
        ["intro continued here."] + ["Method"] + _body("method"),
        ["Results"] + _body("results"),
    ]
    outline = [("Introduction", 0, []), ("Method", 1, []),
               ("Results", 2, [])]
    return _pdf(pages, outline=outline, meta=META)


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _analyze(client, name, data):
    return client.post("/api/import/analyze",
                       data={"zip_file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def _confirm(client, files, **kw):
    body = {"files": files, "import_type": "single_thread",
            "privacy_level": "private", "ai_usage": "none", **kw}
    return client.post("/api/import/confirm", json=body)


# --- Analyze + confirm: own writing ------------------------------------------

def test_outline_becomes_chapters(client):
    r = _analyze(client, "Paper.pdf", _paper())
    assert r.status_code == 200, r.get_json()
    f = r.get_json()["files"][0]
    assert f["source_format"] == "pdf"
    assert f["pages"] == 3 and f["chapters"] == 3
    assert f["modified_at"] == "2021-03-04T05:06:07"
    content = f["content"]
    assert content.startswith("A Study of Things\n\n## Introduction\n\n")
    # The chapter starts at its heading line, mid-page: the line before
    # it stays with the previous chapter; the heading line is not repeated.
    intro = content.split("## Method")[0]
    assert "intro continued here." in intro
    assert content.count("Method") == 1
    assert "## Results" in content

    r = _confirm(client, [f], whose_writing="mine")
    assert r.status_code == 201
    nodes = Node.query.order_by(Node.id).all()
    assert len(nodes) == 4  # title page text (with the file name) + 3
    assert nodes[0].get_content().startswith("# Paper\n\nA Study of Things")
    assert nodes[1].get_content().startswith("## Introduction")
    assert nodes[2].get_content().startswith("## Method\n\nmethod the quick")
    assert nodes[3].get_content().startswith("## Results")
    for a, b in zip(nodes, nodes[1:]):
        assert b.parent_id == a.id
    assert all(n.origin == "pdf" for n in nodes)
    assert all(n.user_id == n.human_owner_id for n in nodes)
    assert nodes[1].created_at.year == 2021
    assert ExternalItem.query.count() == 0


def test_own_entries_encrypted_at_rest(client):
    f = _analyze(client, "Paper.pdf", _paper()).get_json()["files"][0]
    _confirm(client, [f], whose_writing="mine")
    raw = _db.session.execute(
        _db.text("SELECT content FROM node")).scalars().all()
    assert raw and all(
        x.startswith(encryption.ENCRYPTED_PREFIX_V2) for x in raw)
    assert all("quick brown" not in x for x in raw)


def test_single_top_level_entry_uses_its_children(client):
    pages = [["Intro"] + _body("a"), ["Body"] + _body("b")]
    outline = [("The Whole Book", 0, [("Intro", 0), ("Body", 1)])]
    f = _analyze(client, "Book.pdf",
                 _pdf(pages, outline=outline)).get_json()["files"][0]
    assert f["chapters"] == 2
    assert f["content"].startswith("## Intro\n\n")
    assert "## Body" in f["content"]


def test_no_outline_is_one_entry(client):
    pages = [_body("one"), _body("two")]
    f = _analyze(client, "Notes.pdf", _pdf(pages)).get_json()["files"][0]
    assert f["chapters"] == 0
    assert "#" not in f["content"]
    _confirm(client, [f], whose_writing="mine")
    nodes = Node.query.all()
    assert len(nodes) == 1
    assert nodes[0].get_content().startswith(
        "# Notes\n\none the quick brown fox")


def test_no_outline_long_text_splits_by_size(client):
    pages = [[f"{FULL} {i}" for i in range(40)] for _ in range(45)]
    f = _analyze(client, "Long.pdf", _pdf(pages)).get_json()["files"][0]
    assert len(f["content"]) > 100_000
    _confirm(client, [f], whose_writing="mine")
    nodes = Node.query.order_by(Node.id).all()
    assert len(nodes) >= 2
    for a, b in zip(nodes, nodes[1:]):
        assert b.parent_id == a.id


def test_one_outline_entry_is_not_used(client):
    pages = [["Only"] + _body("a"), _body("b")]
    f = _analyze(client, "One.pdf", _pdf(
        pages, outline=[("Only", 0, [])])).get_json()["files"][0]
    assert f["chapters"] == 0
    assert "## " not in f["content"]


def test_missing_dates_fall_back(client):
    f = _analyze(client, "x.pdf", _pdf([_body("a")])).get_json()["files"][0]
    assert f["modified_at"][:4] >= "2026"
    assert f["reference"]["date"] is None
    meta = {"/CreationDate": "D:20190101000000Z"}
    f = _analyze(client, "x.pdf",
                 _pdf([_body("a")], meta=meta)).get_json()["files"][0]
    assert f["modified_at"] == "2019-01-01T00:00:00"


# --- Clean-up ------------------------------------------------------------------

def test_headers_footers_and_page_numbers_removed(client):
    pages = [["Journal of Things, Vol. 3"] + _body(f"p{i}")
             + ["Draft, do not cite", str(i + 1)] for i in range(4)]
    f = _analyze(client, "j.pdf", _pdf(pages)).get_json()["files"][0]
    c = f["content"]
    assert "Journal of Things" not in c
    assert "Draft, do not cite" not in c
    for n in "1234":
        assert f"\n\n{n}\n\n" not in f"\n\n{c}\n\n"
    assert "p0 the quick" in c and "p3 ends here." in c


def test_strip_page_furniture_variants():
    words = ["alpha", "beta", "gamma", "delta", "epsilon"]
    pages = [
        ["Running Title 1", f"{w} one", f"{w} two", f"Page {i} of 5"]
        for i, w in enumerate(words, 1)
    ]
    out = worker.strip_page_furniture(pages)
    assert out == [[f"{w} one", f"{w} two"] for w in words]
    # Page numbers alone are dropped at the edges only, in several forms
    out = worker.strip_page_furniture([
        ["- 4 -", "text", "iv"], ["12", "text 12", "keep me"]])
    assert out == [["text"], ["text 12", "keep me"]]
    # A short document keeps repeated lines (below the page minimum)
    out = worker.strip_page_furniture([["Same", "a"], ["Same", "b"]])
    assert out == [["Same", "a"], ["Same", "b"]]


def test_dehyphenation_and_reflow():
    lines = [
        "In this paper we describe, at some length, a worked exam-",
        "ple of reflowing the lines that were broken up by the PDF",
        "into one paragraph of running text again, as it was written.",
        "Short last line.",
        "",
        "A well-known word keeps its hyphen, a word broken over in-",
        "to the next line is joined, and a hyphen before a Capital-",
        "Letter is kept as it was in the line, since it is a name.",
        "• a bullet starts a new paragraph",
        "#hashtag line",
    ]
    out = worker.reflow(lines, worker.median_line_length(lines))
    paragraphs = out.split("\n\n")
    assert paragraphs[0] == (
        "In this paper we describe, at some length, a worked example of "
        "reflowing the lines that were broken up by the PDF into one "
        "paragraph of running text again, as it was written. Short last "
        "line.")
    assert "A well-known word keeps its hyphen" in paragraphs[1]
    assert "broken over into the next line" in paragraphs[1]
    assert "Capital-Letter" in paragraphs[1]
    assert paragraphs[2] == "- a bullet starts a new paragraph"
    assert paragraphs[3] == "\\#hashtag line"


def test_dehyphenation_across_pages(client):
    pages = [[FULL] * 3 + ["and this long sentence is cut right across the page bre-"],
             ["ak and goes on to its end here."]]
    f = _analyze(client, "h.pdf", _pdf(pages)).get_json()["files"][0]
    assert "page break and goes on" in f["content"]


def test_outline_chapters_rules():
    class E:
        def __init__(self, title, page):
            self.title, self.page = title, page

    page_of = lambda e: e.page  # noqa: E731
    assert worker.outline_chapters(
        [E("A", 0), E("B", 2)], page_of, 3) == [(0, "A"), (2, "B")]
    # all on one page, a single entry, or pages out of range: not usable
    assert worker.outline_chapters([E("A", 0), E("B", 0)], page_of, 3) == []
    assert worker.outline_chapters([E("A", 0)], page_of, 3) == []
    assert worker.outline_chapters([E("A", 0), E("B", 9)], page_of, 3) == []
    # sorted by page, nested children of a multi-entry top level ignored
    assert worker.outline_chapters(
        [E("B", 2), [E("B1", 2)], E("A", 0)], page_of, 3) == [
            (0, "A"), (2, "B")]


# --- Errors and limits ---------------------------------------------------------

def test_scanned_pdf_message(client):
    r = _analyze(client, "scan.pdf", _pdf([[], [], ["7"]]))
    assert r.status_code == 400
    assert r.get_json()["error"] == pdf_import.MSG_SCANNED


def test_encrypted_with_empty_password_opens(client):
    r = _analyze(client, "restricted.pdf",
                 _pdf([_body("secret")], password=""))
    assert r.status_code == 200
    assert "secret the quick" in r.get_json()["files"][0]["content"]


def test_encrypted_with_password_message(client):
    r = _analyze(client, "locked.pdf", _pdf([_body("secret")], password="pw"))
    assert r.status_code == 400
    assert r.get_json()["error"] == pdf_import.MSG_PASSWORD


def test_damaged_pdf_message(client):
    r = _analyze(client, "broken.pdf", b"%PDF-1.4\nnot really a pdf")
    assert r.status_code == 400
    assert r.get_json()["error"] == pdf_import.MSG_DAMAGED


def test_page_cap(client, monkeypatch):
    monkeypatch.setattr(pdf_import, "PDF_MAX_PAGES", 2)
    r = _analyze(client, "big.pdf", _pdf([_body("a")] * 3))
    assert r.status_code == 400
    assert r.get_json()["error"] == (
        "This PDF has 3 pages; Loore imports PDFs of up to 2 pages. Split "
        "it into smaller PDFs and upload them one by one.")


def test_text_cap(client, monkeypatch):
    monkeypatch.setattr(pdf_import, "PDF_MAX_TEXT_CHARS", 100)
    r = _analyze(client, "big.pdf", _pdf([_body("a")]))
    assert r.status_code == 400
    assert r.get_json()["error"].startswith(
        "This PDF has more text than Loore imports at once")


def _script(tmp_path, body):
    path = tmp_path / "worker.py"
    path.write_text(body)
    return str(path)


def test_wall_clock_timeout(client, monkeypatch, tmp_path):
    monkeypatch.setattr(pdf_import, "WORKER_PATH", _script(
        tmp_path, "import time\ntime.sleep(30)\n"))
    monkeypatch.setattr(pdf_import, "PDF_WALL_SECONDS", 0.5)
    r = _analyze(client, "slow.pdf", _pdf([_body("a")]))
    assert r.status_code == 400
    assert r.get_json()["error"] == pdf_import.MSG_TOO_SLOW


def test_cpu_limit(client, monkeypatch, tmp_path):
    # The real worker's set_limits on a child that never stops computing
    src = os.path.abspath(worker.__file__)
    monkeypatch.setattr(pdf_import, "WORKER_PATH", _script(tmp_path, (
        "import runpy, sys\n"
        f"w = runpy.run_path({src!r})\n"
        "w['set_limits'](int(sys.argv[2]), int(sys.argv[3]))\n"
        "while True:\n    pass\n")))
    monkeypatch.setattr(pdf_import, "PDF_CPU_SECONDS", 1)
    r = _analyze(client, "spin.pdf", _pdf([_body("a")]))
    assert r.status_code == 400
    assert r.get_json()["error"] == pdf_import.MSG_TOO_SLOW


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="only Linux enforces RLIMIT_AS")
def test_memory_limit(client, monkeypatch):
    monkeypatch.setattr(pdf_import, "PDF_MEMORY_BYTES", 32 * 1024 * 1024)
    r = _analyze(client, "a.pdf", _pdf([_body("a")]))
    assert r.status_code == 400
    assert r.get_json()["error"] == pdf_import.MSG_TOO_BIG


def test_worker_failure_logs_type_only(client, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(pdf_import, "WORKER_PATH", _script(tmp_path, (
        "import sys\n"
        "sys.stdout.write('{\"error\": \"unreadable\", "
        "\"type\": \"secret words here\"}')\n")))
    r = _analyze(client, "Private Name.pdf", _pdf([_body("a")]))
    assert r.status_code == 400
    assert r.get_json()["error"] == pdf_import.MSG_DAMAGED
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "secret" not in logged and "Private Name" not in logged
    assert "unknown" in logged


def test_unexpected_error_is_json_500(client, monkeypatch, caplog):
    def boom(_):
        raise KeyError("Private Name.pdf")
    monkeypatch.setattr(pdf_import, "_run_worker", boom)
    r = _analyze(client, "Private Name.pdf", _pdf([_body("a")]))
    assert r.status_code == 500
    assert r.get_json()["error"] == pdf_import.PDF_UNEXPECTED_ERROR
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "Private Name" not in logged and "KeyError" in logged


# --- Whose writing ---------------------------------------------------------------

def test_confirm_requires_whose_writing(client):
    f = _analyze(client, "Paper.pdf", _paper()).get_json()["files"][0]
    for whose in (None, "", "maybe"):
        r = _confirm(client, [f], whose_writing=whose)
        assert r.status_code == 400
    assert Node.query.count() == 0 and ExternalItem.query.count() == 0


def test_someone_elses_writing_saved_as_reference(client):
    f = _analyze(client, "2101.00001v2.pdf", _paper()).get_json()["files"][0]
    assert f["reference"] == {
        "title": "A Study of Things", "author": "Jane Doe",
        "date": "2020-01-02T03:04:05", "truncated": False,
        "char_cap": 100_000,
    }
    r = _confirm(client, [f], whose_writing="someone_else")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["saved_as"] == "reference" and body["created"] == 1
    assert Node.query.count() == 0
    items = ExternalItem.query.all()
    assert len(items) == 1
    item = items[0]
    assert body["reference_id"] == item.id
    assert item.source == "document"
    assert item.title == "A Study of Things"
    assert item.author_handle == "Jane Doe"
    assert item.posted_at.isoformat() == "2020-01-02T03:04:05"
    assert item.url is None and item.public_source is False
    assert item.get_content() == f["content"]
    assert "## Method" in item.get_content()
    assert item.content.startswith(encryption.ENCRYPTED_PREFIX_V2)
    assert "quick brown" not in item.content

    # Saving the same PDF again is a no-op
    r = _confirm(client, [f], whose_writing="someone_else")
    assert r.status_code == 200
    assert r.get_json()["skipped"] == 1
    assert ExternalItem.query.count() == 1


def test_reference_text_capped_at_one_entry(client):
    content = "word " * 30_000  # 150k characters
    f = {"name": "Book.pdf", "filename_without_ext": "Book",
         "content": content, "source_format": "pdf",
         "modified_at": "2020-01-01T00:00:00", "reference": {}}
    r = _confirm(client, [f], whose_writing="someone_else")
    assert r.status_code == 201
    assert r.get_json()["truncated"] is True
    item = ExternalItem.query.one()
    assert len(item.get_content()) <= 100_000
    assert item.title == "Book"


def test_reference_title_skips_editor_placeholders():
    assert pdf_import.reference_title(
        "Microsoft Word - draft3.docx", "paper") == "paper"
    assert pdf_import.reference_title("untitled", "paper") == "paper"
    assert pdf_import.reference_title("thesis.tex", "paper") == "paper"
    assert pdf_import.reference_title(None, "paper") == "paper"
    assert pdf_import.reference_title("On Things", "paper") == "On Things"


# --- .doc and zips ---------------------------------------------------------------

def test_old_doc_single_upload_message(client):
    r = _analyze(client, "Old Report.doc", b"\xd0\xcf\x11\xe0 binary")
    assert r.status_code == 400
    assert r.get_json()["error"] == (
        "Old Word (.doc) files can't be imported. Open it in Word or Google "
        "Docs, save it as .docx, and upload that.")


def test_doc_and_pdf_inside_zip_are_listed_errors(client):
    z = _zip({"a.md": "# A\nmd text", "old/b.DOC": b"\xd0\xcf",
              "c.pdf": _paper()})
    r = _analyze(client, "export.zip", z)
    assert r.status_code == 200
    body = r.get_json()
    assert [f["name"] for f in body["files"]] == ["a.md"]
    errors = {e["name"]: e["error"] for e in body["errors"]}
    assert errors["b.DOC"] == pdf_import.MSG_OLD_DOC
    assert errors["c.pdf"] == (
        "PDFs inside a zip aren't imported yet. Upload each PDF on its own.")


def test_zip_with_only_a_pdf_names_it(client):
    r = _analyze(client, "export.zip", _zip({"c.pdf": _paper()}))
    assert r.status_code == 400
    assert "c.pdf: PDFs inside a zip aren't imported yet" in (
        r.get_json()["error"])


def test_other_extensions_still_rejected(client):
    r = _analyze(client, "notes.txt", b"hello")
    assert r.status_code == 400
    assert r.get_json()["error"] == "File must be a .zip, .docx or .pdf file"
