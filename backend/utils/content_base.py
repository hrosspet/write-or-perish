"""A checklist save names the text it was made on (#795).

A tick, or the row "+", on an entry or an artifact sends the page's whole
text. A page left open while the text changed elsewhere (another tab, the
phone, the AI updating an artifact) used to put its older text back with
one box changed. The clients now also send ``base_content``: the text the
page showed before the change. The route saves only when that is still the
stored text; otherwise it saves nothing and answers 409 with
CONTENT_CHANGED_CODE. The page then undoes the tick and asks the person to
reload (Peter, 2026-11-24: a tick never lands on a version the page did not
show).

The text itself is the marker, not a version number: a tick on the
artifacts page edits the latest version in place, so its number stays the
same while its text changes. A request without ``base_content`` (an
installed iPhone build, a tab opened before #795) is saved as before.
"""
from flask import jsonify

CONTENT_CHANGED_CODE = "content_changed"
CONTENT_CHANGED_MESSAGE = (
    "There's a newer version of this text, so this change wasn't saved. "
    "Reload the page before continuing.")


def base_content_of(data):
    """The request's ``base_content``: (text or None, error response or
    None). None means the client sent none (the old request shape)."""
    base = (data or {}).get("base_content")
    if base is None or isinstance(base, str):
        return base, None
    return None, (jsonify({"error": "base_content must be text"}), 400)


def content_changed_response():
    """409: the stored text is no longer the one the page showed. Nothing
    was written."""
    return jsonify({
        "error": CONTENT_CHANGED_MESSAGE,
        "code": CONTENT_CHANGED_CODE,
    }), 409
