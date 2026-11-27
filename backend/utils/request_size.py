"""How large a request body each route takes.

Every route takes at most DEFAULT_MAX_BODY_BYTES (the app's
MAX_CONTENT_LENGTH, backend/config.py). A route that takes larger bodies
says how large with ``@body_limit(...)``, placed right under its
``@bp.route``. Werkzeug refuses a larger body with 413 when the route
first reads it (``request.get_json``, ``request.files``,
``request.get_data``), before any of it is held in memory, and also a
body sent without a Content-Length, once more than the limit has
arrived. The app answers that 413 as JSON (``too_large_response``).

The limit is looked up from the matched route when the body is first
read, so it holds whatever runs before the view (the before_request
hooks) and on every Flask version: the request class below
(``SizedRequest``) is the only place that decides it.

nginx has the same limits per location (configs/nginx.txt), so a larger
body does not reach the app at all; the app does not rely on that.
"""
from flask import Request, current_app, jsonify

MB = 1024 * 1024

# ── Heuristics (named here, listed in the PR) ─────────────────────────────
# Any route without a limit of its own: the JSON the apps send (a reply, an
# edit of an entry, which both apps cap at 100,000 characters, about
# 300 KB; a clipped page, cut to 100,000 characters by the extension; a
# voice turn's transcript, about 110 KB for two hours of speech), and the
# Stripe and GitHub webhooks, whose payloads stay under 1 MB. 2 MB is
# several times the largest of them.
DEFAULT_MAX_BODY_BYTES = 2 * MB
# A whole entry's text in one request: a new entry, a recording saved as an
# entry, a Text mode start, the draft autosave of the same form, and the
# documents edited in place (artifacts, the profile, prompts, the to-do
# list, a share). A paste of any length is sent whole and split on the
# server at 100,000 characters, so a book of a million characters is 1 to
# 4 MB of JSON. 16 MB is four times that.
LONG_TEXT_MAX_BODY_BYTES = 16 * MB
# A file upload: an audio file (routes/nodes.MAX_AUDIO_BYTES, 200 MB), an
# import archive or export (the apps' and nginx's 200 MB limit), and the
# form around it. A file part is written to a temporary file as it
# arrives, not held in memory.
UPLOAD_MAX_BODY_BYTES = 201 * MB
# The markdown/Word/PDF import's confirm: the web page and the iPhone app
# post back the analyze answer's full text (every file's content): up to
# MAX_NOTES_TEXT_BYTES (routes/import_data.py, 64 MB) of notes, with JSON
# escaping (a few percent for notes; a quarter is allowed for: 80 MB),
# the Word text (a few MB) and the files' names and dates (about 5 MB at
# 20,000 files): 90 MB, so 96 MB.
IMPORT_CONFIRM_MAX_BODY_BYTES = 96 * MB

TOO_LARGE_MESSAGE = "This request is larger than Loore accepts."


def body_limit(max_bytes, form=None):
    """Let a route take bodies up to *max_bytes*; with *form*, a
    multipart form (a file upload) may be up to *form* bytes instead.
    Place it directly under the route decorator."""
    def mark(view):
        view.max_body_bytes = max_bytes
        view.max_form_body_bytes = form
        return view
    return mark


def route_limit(request):
    """The body limit of the route *request* matched, or None when it has
    none of its own (or nothing matched)."""
    rule = getattr(request, "url_rule", None)
    if rule is None or not current_app:
        return None
    view = current_app.view_functions.get(rule.endpoint)
    if view is None:
        return None
    form = getattr(view, "max_form_body_bytes", None)
    if form is not None and request.mimetype == "multipart/form-data":
        return form
    return getattr(view, "max_body_bytes", None)


class SizedRequest(Request):
    """Flask's request with the body limit of the matched route."""

    @property
    def max_content_length(self):
        limit = route_limit(self)
        if limit is not None:
            return limit
        if current_app:
            return current_app.config.get("MAX_CONTENT_LENGTH")
        return None


def too_large_response(_error=None):
    """The answer for a body over its route's limit: JSON, as every other
    refusal of the API, so the apps show the text."""
    return jsonify({"error": TOO_LARGE_MESSAGE,
                    "code": "request_too_large"}), 413
