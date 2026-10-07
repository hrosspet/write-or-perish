"""Which Loore app a request came from: the web app or the iPhone app.

Used for the platform label on the GitHub issues Loore files for a user
(Peter's voice review, 2026-10-02): an issue filed from the iPhone app
gets ``ios``, one filed from the web app gets ``web``, and one whose app
is unknown gets no platform label (utils/github.PLATFORM_LABELS).

An issue is filed either by the "Create issue" request itself
(routes/github_issues.py) or during a reply turn, when the user confirms
by voice or text (apply_github_issue). The turn runs in a Celery task with
no request, so the request that starts a reply records its client on the
reply placeholder (CLIENT_MARKER, see llm_nodes.create_llm_placeholder)
and the task reads it from there.

The signals, strongest first:
- An explicit ``X-Loore-Client: ios`` (or ``web``) header. Nothing sends
  it yet; the iPhone app can, to stop relying on its User-Agent.
- The User-Agent. Every browser, the home-screen web app (PWA) and phone
  browsers included, sends one that starts with ``Mozilla/`` -> web. The
  iPhone app makes its API requests with URLSession and sets no
  User-Agent of its own, so it sends the system default
  ``Loore/<build> CFNetwork/<v> Darwin/<v>`` -> ios.
- Anything else (curl, scripts, a background task with no request) ->
  None.
"""
from flask import has_request_context, request

IOS = "ios"
WEB = "web"
CLIENTS = (IOS, WEB)

CLIENT_HEADER = "X-Loore-Client"

# tool_calls_meta entry on a reply placeholder: {"name": "_client",
# "client": "ios" | "web"}. Underscore names are hidden by both apps, like
# "_mode". Only the reply task reads it (every run of it: a batch poll or a
# resumed run starts from the node), so it stays on the node; the node
# payloads that go to other users leave it out (without_client_marker).
CLIENT_MARKER = "_client"


def client_from_headers(headers):
    """'ios', 'web', or None for a request's headers (any mapping with
    .get, such as request.headers)."""
    explicit = (headers.get(CLIENT_HEADER) or "").strip().lower()
    if explicit in CLIENTS:
        return explicit
    ua = (headers.get("User-Agent") or "").strip()
    if ua.startswith("Mozilla/"):
        return WEB
    if "CFNetwork/" in ua and "Darwin/" in ua:
        return IOS
    return None


def request_client():
    """The client of the current request, or None outside a request."""
    if not has_request_context():
        return None
    return client_from_headers(request.headers)


def without_client_marker(meta):
    """*meta* (a tool_calls_meta list as the API returns it) without the
    "_client" entry. Which app the author uses is not for the other users
    who can see their node."""
    if not isinstance(meta, list):
        return meta
    return [m for m in meta
            if not (isinstance(m, dict) and m.get("name") == CLIENT_MARKER)]
