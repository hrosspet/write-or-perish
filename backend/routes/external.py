"""External content routes (#155 component 2 / Download substrate).

- Community Archive fetch (public API, any user, no credentials)
- X bookmarks: OAuth2 PKCE connect + sync (env-gated by X_CLIENT_ID
  until pay-per-use credits are configured) and a JSON import fallback
- Listing imported references
"""
import base64
import hashlib
import json
import re
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

    def _enrich_content(entry, text):
        """Fold the exporter's enrichment fields (quoted tweet, link card,
        media, expanded links) into the stored content — the embedding and
        the quote card both need the bookmark's actual meaning, which for
        link/media tweets isn't in the surface text."""
        parts = []
        # Trailing t.co media stub is redundant once media is captured.
        media = entry.get("media") or []
        if media:
            text = re.sub(r"\s*https://t\.co/\S+$", "", text or "")
        if text:
            parts.append(text)
        quoted = entry.get("quoted") or {}
        if isinstance(quoted, dict) and quoted.get("text"):
            q_author = quoted.get("author") or "unknown"
            parts.append(f"[Quoting @{q_author}: {quoted['text']}]")
        card = entry.get("card") or {}
        if isinstance(card, dict) and (card.get("title")
                                       or card.get("description")):
            bits = " — ".join(
                b for b in (card.get("title"), card.get("description"))
                if b)
            domain = f" ({card['domain']})" if card.get("domain") else ""
            parts.append(f"[Link: {bits}{domain}]")
        for m in media:
            if not isinstance(m, dict):
                continue
            kind = m.get("type") or "media"
            alt = f": {m['alt']}" if m.get("alt") else ""
            parts.append(f"[{kind}{alt}]")
        links = [ln for ln in (entry.get("links") or [])
                 if isinstance(ln, str)]
        # Links already present in the (expanded) text add nothing.
        fresh_links = [ln for ln in links if ln not in (text or "")]
        if fresh_links:
            parts.append("[Links: " + " ".join(fresh_links) + "]")
        return "\n".join(parts)

    def _normalize(entry):
        if not isinstance(entry, dict):
            return None
        text = entry.get("text") or entry.get("full_text")
        tweet_id = entry.get("id") or entry.get("tweet_id")
        url = entry.get("url")
        if not tweet_id and url and "/status/" in str(url):
            tweet_id = str(url).rstrip("/").split("/status/")[-1].split("?")[0]
        if not tweet_id:
            return None
        text = _enrich_content(entry, str(text) if text else "")
        if not text:
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
            "content": text,
            "url": url or f"https://twitter.com/i/status/{tweet_id}",
            "posted_at": posted_at,
        }

    items = [n for n in (_normalize(e) for e in payload) if n]
    created, skipped = _upsert_items(
        current_user.id, "twitter_bookmark", items)
    if created:
        from backend.tasks.external_digest import rebuild_external_digest
        rebuild_external_digest.delay(current_user.id)
    return jsonify({
        "created": created, "skipped": skipped,
        "unrecognized": len(payload) - len(items),
    }), 200


# ── X OAuth2 PKCE connect (env-gated) ────────────────────────────────────

def _frontend_url(path):
    """Absolute SPA URL for post-OAuth redirects. The callback runs on the
    backend origin; a relative redirect 404s in dev (SPA lives on another
    port) and only works in prod by the grace of same-origin nginx."""
    from flask import current_app
    base = (current_app.config.get("FRONTEND_URL") or "").rstrip("/")
    return f"{base}{path}"


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
        "revoked": bool(account and account.revoked_at),
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
        return redirect(_frontend_url("/import?x_connect=failed"))

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
    except requests.RequestException as exc:
        body = ""
        if getattr(exc, "response", None) is not None:
            body = f" — response: {exc.response.text[:500]}"
        current_app.logger.exception("X OAuth callback failed%s", body)
        return redirect(_frontend_url("/import?x_connect=failed"))

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
    account.revoked_at = None  # fresh consent supersedes any revocation
    db.session.commit()
    return redirect(_frontend_url("/import?x_connect=ok"))


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
