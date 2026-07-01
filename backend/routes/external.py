"""External content routes (#155 component 2 / Download substrate).

- Community Archive fetch (public API, any user, no credentials)
- X bookmarks: OAuth2 PKCE connect + sync (env-gated by X_CLIENT_ID
  until pay-per-use credits are configured) and a JSON import fallback
- Listing imported references
"""
import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta

import requests
from flask import (
    Blueprint, current_app, jsonify, redirect, request, session,
)
from flask_login import current_user, login_required

from backend.extensions import db
from backend.models import ExternalAccount, ExternalItem
from backend.utils.timefmt import iso_utc

external_bp = Blueprint("external_bp", __name__)

X_AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
X_SCOPES = "tweet.read users.read bookmark.read offline.access"


def _serialize_item(item):
    content = item.get_content() or ""
    return {
        "id": item.id,
        "source": item.source,
        "author_handle": item.author_handle,
        "preview": content[:280] + ("…" if len(content) > 280 else ""),
        "url": item.url,
        "posted_at": iso_utc(item.posted_at) if item.posted_at else None,
        "fetched_at": iso_utc(item.fetched_at),
    }


@external_bp.route("/items", methods=["GET"])
@login_required
def list_items():
    source = request.args.get("source")
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)

    query = ExternalItem.query.filter_by(user_id=current_user.id)
    if source:
        query = query.filter_by(source=source)
    total = query.count()
    items = query.order_by(
        ExternalItem.posted_at.desc().nullslast(),
        ExternalItem.id.desc(),
    ).offset((page - 1) * per_page).limit(per_page).all()

    counts = dict(
        db.session.query(ExternalItem.source, db.func.count())
        .filter_by(user_id=current_user.id)
        .group_by(ExternalItem.source).all()
    )
    return jsonify({
        "items": [_serialize_item(i) for i in items],
        "total": total,
        "counts": counts,
    }), 200


@external_bp.route("/community-archive/fetch", methods=["POST"])
@login_required
def fetch_community_archive_route():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lstrip("@")
    if not username:
        return jsonify({"error": "username is required"}), 400

    from backend.tasks.external_sync import fetch_community_archive
    task = fetch_community_archive.delay(
        current_user.id, username,
        max_items=min(int(data.get("max_items", 2000)), 10000))
    return jsonify({"task_id": task.id, "status": "pending"}), 202


@external_bp.route("/bookmarks/import", methods=["POST"])
@login_required
def import_bookmarks_json():
    """JSON import fallback for X bookmarks (no API credits needed).

    Accepts a flexible array of objects; recognizes common shapes from
    bookmark-export browser scripts: {id|tweet_id|url, text|full_text,
    author|username|screen_name, created_at}.
    """
    payload = request.get_json(silent=True)
    if payload is None and "file" in request.files:
        try:
            payload = json.load(request.files["file"].stream)
        except (ValueError, UnicodeDecodeError):
            return jsonify({"error": "File is not valid JSON"}), 400
    if isinstance(payload, dict):
        payload = payload.get("bookmarks") or payload.get("data")
    if not isinstance(payload, list):
        return jsonify({"error": (
            "Provide a JSON array of bookmarks (or {bookmarks: [...]}).")
        }), 400

    from backend.tasks.external_sync import _upsert_items

    def _normalize(entry):
        if not isinstance(entry, dict):
            return None
        text = entry.get("text") or entry.get("full_text")
        tweet_id = entry.get("id") or entry.get("tweet_id")
        url = entry.get("url")
        if not tweet_id and url and "/status/" in str(url):
            tweet_id = str(url).rstrip("/").split("/status/")[-1].split("?")[0]
        if not tweet_id or not text:
            return None
        handle = (entry.get("author") or entry.get("username")
                  or entry.get("screen_name"))
        posted_at = None
        if entry.get("created_at"):
            try:
                posted_at = datetime.fromisoformat(
                    str(entry["created_at"]).replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (ValueError, TypeError):
                pass
        return {
            "external_id": str(tweet_id),
            "author_handle": handle,
            "content": str(text),
            "url": url or f"https://twitter.com/i/status/{tweet_id}",
            "posted_at": posted_at,
        }

    items = [n for n in (_normalize(e) for e in payload) if n]
    created, skipped = _upsert_items(
        current_user.id, "twitter_bookmark", items)
    return jsonify({
        "created": created, "skipped": skipped,
        "unrecognized": len(payload) - len(items),
    }), 200


# ── X OAuth2 PKCE connect (env-gated) ────────────────────────────────────

def _x_configured():
    return bool(current_app.config.get("X_CLIENT_ID"))


@external_bp.route("/twitter/status", methods=["GET"])
@login_required
def twitter_status():
    account = ExternalAccount.query.filter_by(
        user_id=current_user.id, provider="twitter").first()
    return jsonify({
        "configured": _x_configured(),
        "connected": bool(account and account.get_access_token()),
        "handle": account.handle if account else None,
        "last_synced_at": (iso_utc(account.last_synced_at)
                           if account and account.last_synced_at else None),
    }), 200


@external_bp.route("/twitter/connect", methods=["GET"])
@login_required
def twitter_connect():
    if not _x_configured():
        return jsonify({"error": (
            "X API is not configured (set X_CLIENT_ID / X_REDIRECT_URI)."
        )}), 503

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    session["x_oauth"] = {"verifier": verifier, "state": state}

    from urllib.parse import urlencode
    params = urlencode({
        "response_type": "code",
        "client_id": current_app.config["X_CLIENT_ID"],
        "redirect_uri": current_app.config["X_REDIRECT_URI"],
        "scope": X_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return redirect(f"{X_AUTHORIZE_URL}?{params}")


@external_bp.route("/twitter/callback", methods=["GET"])
@login_required
def twitter_callback():
    stash = session.pop("x_oauth", None)
    if (not stash or request.args.get("state") != stash["state"]
            or not request.args.get("code")):
        return redirect("/import?x_connect=failed")

    data = {
        "grant_type": "authorization_code",
        "code": request.args["code"],
        "redirect_uri": current_app.config["X_REDIRECT_URI"],
        "client_id": current_app.config["X_CLIENT_ID"],
        "code_verifier": stash["verifier"],
    }
    auth = None
    if current_app.config.get("X_CLIENT_SECRET"):
        auth = (current_app.config["X_CLIENT_ID"],
                current_app.config["X_CLIENT_SECRET"])
    try:
        token_resp = requests.post(
            X_TOKEN_URL, data=data, auth=auth, timeout=30)
        token_resp.raise_for_status()
        tokens = token_resp.json()

        me = requests.get(
            "https://api.twitter.com/2/users/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            timeout=30,
        )
        me.raise_for_status()
        me_data = me.json().get("data", {})
    except requests.RequestException:
        current_app.logger.exception("X OAuth callback failed")
        return redirect("/import?x_connect=failed")

    account = ExternalAccount.query.filter_by(
        user_id=current_user.id, provider="twitter").first()
    if account is None:
        account = ExternalAccount(
            user_id=current_user.id, provider="twitter")
        db.session.add(account)
    account.set_tokens(tokens["access_token"], tokens.get("refresh_token"))
    if tokens.get("expires_in"):
        account.token_expires_at = (
            datetime.utcnow() + timedelta(seconds=int(tokens["expires_in"])))
    account.external_user_id = me_data.get("id")
    account.handle = me_data.get("username")
    db.session.commit()
    return redirect("/import?x_connect=ok")


@external_bp.route("/twitter/sync", methods=["POST"])
@login_required
def twitter_sync():
    account = ExternalAccount.query.filter_by(
        user_id=current_user.id, provider="twitter").first()
    if account is None or not account.get_access_token():
        return jsonify({"error": "X account not connected"}), 400
    from backend.tasks.external_sync import sync_twitter_bookmarks
    task = sync_twitter_bookmarks.delay(current_user.id)
    return jsonify({"task_id": task.id, "status": "pending"}), 202


@external_bp.route("/recommendations", methods=["GET"])
@login_required
def recommendations():
    """Top-3 external items relevant to the thread the user is writing in
    (Download PoC — dark behind DOWNLOAD_V1).

    The query is composed server-side from the node's thread tail + the
    user's intentions + profile (AI-visible content only) and ranked by
    semantic relevance over the user's imported items. Returns items only
    for their owner; empty list when nothing clears the score floor.
    """
    if not current_app.config.get("DOWNLOAD_V1", False):
        return jsonify({"error": "Not found"}), 404

    node_id = request.args.get("node_id", type=int)
    if not node_id:
        return jsonify({"error": "node_id is required"}), 400

    from backend.models import Node
    node = Node.query.get(node_id)
    # human_owner_id is null on legacy rows — fall back to user_id (same
    # convention as the embedding-owner helper).
    owner_id = (node.human_owner_id or node.user_id) if node else None
    if not node or owner_id != current_user.id:
        return jsonify({"error": "Not found"}), 404

    from backend.utils.api_keys import get_openai_chat_key
    api_key = get_openai_chat_key(current_app.config)
    if not api_key:
        return jsonify({"items": []}), 200

    from backend.utils.recommendations import recommend_external_items
    try:
        items = recommend_external_items(
            current_user.id, node, api_key,
            k=current_app.config.get("DOWNLOAD_TOP_K", 3),
        )
        db.session.commit()  # persist the query-embed cost log
    except Exception:  # noqa: BLE001 — recommendations must never break a page
        current_app.logger.warning(
            "external recommendations failed", exc_info=True)
        return jsonify({"items": []}), 200
    return jsonify({"items": items}), 200
