"""Word (.docx) import: /api/import/analyze + /api/import/confirm.

Fixtures are built by hand (minimal OOXML zips), so python-docx is not
needed. Encryption is ON (KMS faked) to check imported text is encrypted
at rest exactly like markdown imports.
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

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node            # noqa: E402
import backend.models as _real_backend_models    # noqa: E402
from backend.utils import docx_import, encryption  # noqa: E402


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


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CT = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
      'package/2006/content-types"><Default Extension="rels" ContentType='
      '"application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/'
      'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
      '</Types>')
RELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship '
        'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>')


def _p(text, style=None, bold=False):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{ppr}<w:r>{rpr}<w:t>{text}</w:t></w:r></w:p>"


def _docx(paragraphs, modified="2021-03-04T05:06:07Z", extra=None,
          doctype=False):
    """Minimal .docx bytes. paragraphs: plain text or raw w: xml."""
    body = "".join(p if p.startswith("<") else _p(p) for p in paragraphs)
    doc = (('<!DOCTYPE x [<!ENTITY a "b">]>' if doctype else "")
           + f'<w:document {W}><w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", doc)
        if modified:
            z.writestr(
                "docProps/core.xml",
                '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://'
                'schemas.openxmlformats.org/package/2006/metadata/core-'
                'properties" xmlns:dcterms="http://purl.org/dc/terms/">'
                f'<dcterms:modified>{modified}</dcterms:modified>'
                '</cp:coreProperties>')
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _thesis(chapters=3):
    ps = [_p("Preface text")]
    for i in range(1, chapters + 1):
        ps.append(_p(f"Chapter {i}", "Heading1"))
        ps.append(_p(f"Body of chapter {i}", None, bold=True))
        ps.append(_p("Sub", "Heading2"))
        ps.append(_p("Sub body"))
    return _docx(ps)


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


def test_single_docx_analyze_converts_headings_and_date(client):
    r = _analyze(client, "Thesis.docx", _thesis())
    assert r.status_code == 200
    f = r.get_json()["files"][0]
    assert f["source_format"] == "docx"
    assert f["modified_at"] == "2021-03-04T05:06:07"
    assert "# Chapter 1" in f["content"]
    assert "## Sub" in f["content"]
    assert "**Body of chapter 1**" in f["content"]


def test_docx_with_headings_becomes_chapter_thread(client):
    f = _analyze(client, "Thesis.docx", _thesis(3)).get_json()["files"][0]
    r = _confirm(client, [f])
    assert r.status_code == 201
    nodes = Node.query.order_by(Node.id).all()
    assert len(nodes) == 4  # preface (with filename H1) + 3 chapters
    assert nodes[0].parent_id is None
    for a, b in zip(nodes, nodes[1:]):
        assert b.parent_id == a.id
    assert nodes[0].get_content().startswith("# Thesis\n\nPreface text")
    assert nodes[2].get_content().startswith("# Chapter 2")
    assert "## Sub" in nodes[2].get_content()
    assert all(n.origin == "docx" for n in nodes)
    assert all(n.user_id == n.human_owner_id for n in nodes)
    assert nodes[1].created_at < nodes[2].created_at < nodes[3].created_at
    assert nodes[1].created_at.year == 2021


def test_docx_content_encrypted_at_rest(client):
    f = _analyze(client, "Thesis.docx", _thesis(2)).get_json()["files"][0]
    _confirm(client, [f])
    raw = _db.session.execute(
        _db.text("SELECT content FROM node")).scalars().all()
    assert raw
    assert all(x.startswith(encryption.ENCRYPTED_PREFIX_V2) for x in raw)
    assert all("Chapter" not in x for x in raw)


def test_docx_without_headings_single_node(client):
    data = _docx(["First paragraph", "Second paragraph"])
    f = _analyze(client, "Notes.docx", data).get_json()["files"][0]
    _confirm(client, [f])
    nodes = Node.query.all()
    assert len(nodes) == 1
    assert nodes[0].get_content() == (
        "# Notes\n\nFirst paragraph\n\nSecond paragraph")


def test_docx_without_headings_long_splits_chain(client):
    data = _docx(["word " * 40 + str(i) for i in range(1, 4000)])
    f = _analyze(client, "Long.docx", data).get_json()["files"][0]
    assert len(f["content"]) > 100_000
    _confirm(client, [f])
    nodes = Node.query.order_by(Node.id).all()
    assert len(nodes) >= 2
    for a, b in zip(nodes, nodes[1:]):
        assert b.parent_id == a.id


def test_single_heading_is_not_split(client):
    data = _docx([_p("Only one", "Heading1"), "text"])
    f = _analyze(client, "One.docx", data).get_json()["files"][0]
    _confirm(client, [f])
    assert Node.query.count() == 1


def test_missing_modified_falls_back_to_upload_time(client):
    f = _analyze(client, "x.docx",
                 _docx(["hello"], modified=None)).get_json()["files"][0]
    assert f["modified_at"][:4] >= "2025"


def test_docx_inside_zip_mixed_with_md(client):
    z = _zip({"a.md": "# A\nmd text", "sub/Thesis.docx": _thesis(2),
              "__MACOSX/._x.docx": b"junk"})
    r = _analyze(client, "export.zip", z)
    assert r.status_code == 200
    body = r.get_json()
    assert body["total_files"] == 2 and body["errors"] == []
    kinds = {f["name"]: f.get("source_format") for f in body["files"]}
    assert kinds == {"a.md": None, "Thesis.docx": "docx"}
    _confirm(client, body["files"], import_type="separate_nodes")
    assert {n.origin for n in Node.query.all()} == {"markdown", "docx"}


def test_bad_docx_in_zip_named_and_rest_imports(client):
    z = _zip({"good.md": "hello", "broken.docx": b"not a zip",
              "doctype.docx": _docx(["x"], doctype=True),
              "macro.docm": _docx(["x"])})
    body = _analyze(client, "e.zip", z).get_json()
    assert [f["name"] for f in body["files"]] == ["good.md"]
    errs = {e["name"]: e["error"] for e in body["errors"]}
    assert set(errs) == {"broken.docx", "doctype.docx", "macro.docm"}
    assert ".docm" in errs["macro.docm"]


def test_zip_with_only_bad_docx_is_400_naming_file(client):
    r = _analyze(client, "e.zip", _zip({"broken.docx": b"nope"}))
    assert r.status_code == 400
    assert "broken.docx" in r.get_json()["error"]


def test_doctype_rejected(client):
    r = _analyze(client, "evil.docx", _docx(["x"], doctype=True))
    assert r.status_code == 400
    assert "evil.docx" in r.get_json()["error"]


def test_doctype_in_other_part_rejected(client):
    data = _docx(["x"], extra={"word/styles.xml": '<!DOCTYPE s []><s/>'})
    assert _analyze(client, "e.docx", data).status_code == 400


def test_oversize_document_xml_rejected(client, monkeypatch):
    monkeypatch.setattr(docx_import, "MAX_PARSED_XML_BYTES", 200)
    r = _analyze(client, "big.docx", _docx(["x " * 500]))
    assert r.status_code == 400
    assert "too large" in r.get_json()["error"]


def test_total_uncompressed_rejected(client, monkeypatch):
    monkeypatch.setattr(docx_import, "MAX_TOTAL_UNCOMPRESSED_BYTES", 1000)
    assert _analyze(client, "a.docx", _docx(["x " * 500])).status_code == 400


def test_compression_ratio_rejected(client, monkeypatch):
    monkeypatch.setattr(docx_import, "MIN_BYTES_FOR_RATIO_CHECK", 100)
    monkeypatch.setattr(docx_import, "MAX_COMPRESSION_RATIO", 2)
    data = _docx(["x"], extra={"word/media/a.bin": b"0" * 200_000})
    assert _analyze(client, "b.docx", data).status_code == 400


def test_docm_rejected_by_extension(client):
    r = _analyze(client, "m.docm", _docx(["x"]))
    assert r.status_code == 400
    assert ".docm" in r.get_json()["error"]


def test_not_a_zip_or_not_word_rejected(client):
    assert _analyze(client, "a.docx", b"plain").status_code == 400
    assert _analyze(client, "b.docx", _zip({"x.txt": "y"})).status_code == 400


def test_convert_table_and_no_escaping():
    tbl = ("<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>"
           "<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr>"
           "<w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc>"
           "<w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc></w:tr></w:tbl>")
    bm = ('<w:p><w:bookmarkStart w:id="0" w:name="b"/><w:r><w:t>v1.2 - end.'
          '</w:t></w:r><w:bookmarkEnd w:id="0"/></w:p>')
    md, _ = docx_import.convert_docx(io.BytesIO(_docx([tbl, bm])))
    assert "| A | B |" in md and "| 1 | 2 |" in md
    assert "v1.2 - end." in md
    assert "<a" not in md and "\\." not in md


def test_split_chapters_levels():
    md = "# T\n\n## a\n\nx\n\n## b\n\ny"
    assert docx_import.split_chapters(md) == ["# T", "## a\n\nx", "## b\n\ny"]
    assert docx_import.split_chapters("text only") == ["text only"]


def test_clean_html_image_placeholders_and_anchors():
    html = ('<p><img src="" alt="A chart"/> <img src="" alt=""/> '
            '<a id="x"></a>text <sup><a href="#footnote-1" id="footnote-ref-1">'
            '[1]</a></sup></p><ol><li id="footnote-1"><p>Note '
            '<a href="#footnote-ref-1">\u2191</a></p></li></ol>')
    md = docx_import.markdownify(docx_import._clean_html(html),
                                 heading_style="ATX", bullets="-")
    assert "[image: A chart]" in md and "[image]" in md
    assert "<a" not in md and "\u2191" not in md


DOC_RELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship '
            'Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/{kind}" Target="{target}"/></Relationships>')


def _with_related_part(kind, target, xml):
    return _docx(["x"], extra={
        "word/_rels/document.xml.rels": DOC_RELS.format(kind=kind, target=target),
        "word/" + target: xml,
    })


def test_part_reached_only_through_relationships_is_checked(client, monkeypatch):
    sty = ('<w:styles xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"/>')
    ok = _with_related_part("styles", "odd/Name.xml", sty)
    assert _analyze(client, "ok.docx", ok).status_code == 200
    bad = _with_related_part("footnotes", "odd/Name.xml",
                             "<!DOCTYPE f []>" + sty)
    assert _analyze(client, "bad.docx", bad).status_code == 400
    monkeypatch.setattr(docx_import, "MAX_PARSED_XML_BYTES", 3000)
    big = _with_related_part("styles", "odd/Name.xml",
                             sty + "<!-- " + "a" * 4000 + " -->")
    r = _analyze(client, "big.docx", big)
    assert r.status_code == 400 and "big.docx" in r.get_json()["error"]


def test_combined_cap_across_docx_in_one_zip(client, monkeypatch):
    one = _docx(["hello " * 100])
    with zipfile.ZipFile(io.BytesIO(one)) as z:
        size = sum(i.file_size for i in z.infolist()
                   if i.filename.endswith(".xml") or i.filename.endswith(".rels"))
    monkeypatch.setattr(docx_import, "MAX_PARSED_XML_BYTES", int(size * 1.5))
    body = _analyze(client, "e.zip", _zip({"a.docx": one, "b.docx": one})).get_json()
    assert [f["name"] for f in body["files"]] == ["a.docx"]
    assert [e["name"] for e in body["errors"]] == ["b.docx"]
    assert _analyze(client, "one.docx", one).status_code == 200


def test_reimport_of_deleted_docx_restores_whole_document(client):
    import datetime
    f = _analyze(client, "Thesis.docx", _thesis(3)).get_json()["files"][0]
    _confirm(client, [f])
    for n in Node.query.all():
        n.deleted_at = datetime.datetime.utcnow()
    _db.session.commit()
    assert _confirm(client, [f]).status_code == 409
    r = _confirm(client, [f], on_deleted="restore")
    assert r.status_code == 201
    live = Node.query.filter(Node.deleted_at.is_(None)).order_by(Node.id).all()
    assert len(live) == 4
    assert live[1].get_content().startswith("# Chapter 1")
    assert live[3].get_content().startswith("# Chapter 3")
    for a, b in zip(live, live[1:]):
        assert b.parent_id == a.id
