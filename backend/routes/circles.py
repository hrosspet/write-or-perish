"""Circles routes: the owner's circles, sharing previews, "Shared with
you" and the per-reader mute (dark behind CIRCLES_V1 / CIRCLES_USER_IDS;
every route answers 404 while Circles is off for the user).

People are found by their exact Loore name only: there is no search, no
directory and no lookup by email, and every name that cannot be shared
with gets the same answer. Emails are never accepted or returned.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import func

from backend.extensions import db
from backend.models import Circle, CircleMember, CircleMute, Node, User
from backend.utils import circles as cz
from backend.utils.privacy import readable_shared_entries
from backend.utils.timefmt import iso_utc

circles_bp = Blueprint("circles_bp", __name__)

# ── Heuristics (named in the PR) ────────────────────────────────────────
# Entries per page of "Shared with you" (each costs one decrypt for its
# title and preview; the unwraps are fetched together).
SHARED_PAGE_SIZE = 30
PREVIEW_CHARS = 160
TITLE_CHARS = 80


@circles_bp.before_request
def _gate():
    if not current_user.is_authenticated:
        return jsonify({"error": "Authentication required"}), 401
    if not cz.circles_enabled(current_user):
        return jsonify({"error": "Not found"}), 404
    return None


def _serialize_circles(circles):
    ids = [c.id for c in circles]
    members = cz.circle_members(ids)
    counts = cz.shared_entry_counts(ids)
    return [{
        "id": c.id,
        "name": c.name,
        "members": [{"username": u.username,
                     "only_new": m.entries_from is not None}
                    for m, u in members[c.id]],
        "shared_count": counts.get(c.id, 0),
    } for c in circles]


def _own_circles():
    return (Circle.query.filter_by(user_id=current_user.id)
            .order_by(Circle.created_at.asc(), Circle.id.asc()).all())


def _clean_circle_name(data, circle_id=None):
    """(name, error response)."""
    name = " ".join(((data or {}).get("name") or "").split())
    if not name:
        return None, (jsonify({"error": "Give the circle a name."}), 400)
    if len(name) > cz.MAX_CIRCLE_NAME_CHARS:
        return None, (jsonify({
            "error": f"A circle's name can have up to "
                     f"{cz.MAX_CIRCLE_NAME_CHARS} characters."}), 400)
    clash = Circle.query.filter(
        Circle.user_id == current_user.id,
        func.lower(Circle.name) == name.lower())
    if circle_id is not None:
        clash = clash.filter(Circle.id != circle_id)
    if clash.first() is not None:
        return None, (jsonify({
            "error": "You already have a circle with that name."}), 409)
    return name, None


@circles_bp.route("", methods=["GET"])
@login_required
def list_circles():
    return jsonify({"circles": _serialize_circles(_own_circles())}), 200


@circles_bp.route("", methods=["POST"])
@login_required
def create_circle():
    name, err = _clean_circle_name(request.get_json(silent=True))
    if err:
        return err
    if Circle.query.filter_by(
            user_id=current_user.id).count() >= cz.MAX_CIRCLES_PER_USER:
        return jsonify({
            "error": f"You can have up to {cz.MAX_CIRCLES_PER_USER} "
                     "circles."}), 400
    circle = Circle(user_id=current_user.id, name=name)
    db.session.add(circle)
    db.session.commit()
    return jsonify({"circle": _serialize_circles([circle])[0]}), 201


@circles_bp.route("/<int:circle_id>", methods=["PUT"])
@login_required
def rename_circle(circle_id):
    circle = cz.own_circle(circle_id, current_user.id)
    if circle is None:
        return jsonify({"error": "Not found"}), 404
    name, err = _clean_circle_name(request.get_json(silent=True), circle.id)
    if err:
        return err
    circle.name = name
    db.session.commit()
    return jsonify({"circle": _serialize_circles([circle])[0]}), 200


@circles_bp.route("/<int:circle_id>", methods=["DELETE"])
@login_required
def delete_circle(circle_id):
    circle = cz.own_circle(circle_id, current_user.id)
    if circle is None:
        return jsonify({"error": "Not found"}), 404
    cz.delete_circle(circle)
    db.session.commit()
    return jsonify({"deleted": True}), 200


# ── People ──────────────────────────────────────────────────────────────

def _lookup(username):
    """(person, error response) for one typed name."""
    if cz.lookup_blocked(current_user.id):
        return None, (jsonify({
            "error": "Too many names that nobody has. Try again in an "
                     "hour.", "code": "circle_lookup_limit"}), 429)
    person = cz.find_person(username, current_user.id)
    if person is None:
        cz.record_lookup_miss(current_user.id)
        return None, (jsonify({"error": cz.NO_SUCH_NAME,
                               "code": "circle_unknown_name"}), 404)
    return person, None


@circles_bp.route("/<int:circle_id>/members/preview", methods=["POST"])
@login_required
def preview_member(circle_id):
    """Before adding: does the name exist, and how many entries are
    already shared with the circle (the add dialog states the count)."""
    circle = cz.own_circle(circle_id, current_user.id)
    if circle is None:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    person, err = _lookup(data.get("username"))
    if err:
        return err
    already = CircleMember.query.filter_by(
        circle_id=circle.id, user_id=person.id).first() is not None
    return jsonify({
        "username": person.username,
        "circle": circle.name,
        "already_member": already,
        "shared_count": cz.shared_entry_counts([circle.id]).get(circle.id, 0),
    }), 200


@circles_bp.route("/<int:circle_id>/members", methods=["POST"])
@login_required
def add_member(circle_id):
    """Add a person. ``include_existing`` true ("Add <name>"): they also
    read the entries already shared with the circle. False ("Only new
    entries"): only what is shared with it from now on."""
    circle = cz.own_circle(circle_id, current_user.id)
    if circle is None:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("include_existing"), bool):
        return jsonify({"error": "include_existing must be true or false"}), 400
    person, err = _lookup(data.get("username"))
    if err:
        return err
    if CircleMember.query.filter_by(
            circle_id=circle.id, user_id=person.id).first() is not None:
        return jsonify({
            "error": f"{person.username} is already in {circle.name}."}), 409
    if CircleMember.query.filter_by(
            circle_id=circle.id).count() >= cz.MAX_MEMBERS_PER_CIRCLE:
        return jsonify({
            "error": f"A circle can have up to {cz.MAX_MEMBERS_PER_CIRCLE} "
                     "people."}), 400
    now = datetime.utcnow()
    include = data["include_existing"]
    db.session.add(CircleMember(
        circle_id=circle.id, user_id=person.id, created_at=now,
        entries_from=None if include else now))
    db.session.flush()
    cz.forget_circle_access()
    if include:
        # One note, for the newest entry now open to them: a note per
        # entry would arrive as a pile for a circle with a history.
        from backend.models import NodeCircle
        newest = (Node.query.join(NodeCircle, NodeCircle.node_id == Node.id)
                  .filter(NodeCircle.circle_id == circle.id,
                          *cz._alive_shared_entry())
                  .order_by(Node.created_at.desc(), Node.id.desc()).first())
        if newest is not None:
            cz.note_shared(newest, {person.id})
    db.session.commit()
    return jsonify({"circle": _serialize_circles([circle])[0]}), 201


@circles_bp.route("/<int:circle_id>/members/<username>", methods=["DELETE"])
@login_required
def remove_member(circle_id, username):
    """Remove a person: the circle's entries are closed to them from
    this moment. Found among the circle's members only, so a removed
    account that can no longer be looked up can still be removed."""
    circle = cz.own_circle(circle_id, current_user.id)
    if circle is None:
        return jsonify({"error": "Not found"}), 404
    row = (db.session.query(CircleMember)
           .join(User, User.id == CircleMember.user_id)
           .filter(CircleMember.circle_id == circle.id,
                   func.lower(User.username) == cz.clean_name(username).lower())
           .first())
    if row is None:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(row)
    cz.forget_circle_access()
    db.session.commit()
    return jsonify({"circle": _serialize_circles([circle])[0]}), 200


@circles_bp.route("/audience-preview", methods=["POST"])
@login_required
def audience_preview():
    """Who would read an entry shared with these circles and names: the
    sentence the form shows before saving."""
    data = request.get_json(silent=True) or {}
    try:
        circles, people = cz.resolve_audience(
            current_user, data.get("circle_ids"), data.get("usernames"))
    except cz.AudienceError as e:
        return e.response()
    return jsonify({
        "readers": cz.preview_readers(circles, people, current_user.id),
    }), 200


# ── The reader's side ───────────────────────────────────────────────────

def _title_and_preview(text):
    """An entry's title (its first line, without Markdown heading marks)
    and a short preview of what follows."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "", ""
    first = lines[0].lstrip("#").strip()
    rest = " ".join(lines[1:])
    if len(first) > TITLE_CHARS:
        cut = first[:TITLE_CHARS].rsplit(" ", 1)[0] or first[:TITLE_CHARS]
        rest = (first[len(cut):].strip() + " " + rest).strip()
        first = cut + "…"
    if len(rest) > PREVIEW_CHARS:
        rest = (rest[:PREVIEW_CHARS].rsplit(" ", 1)[0]
                or rest[:PREVIEW_CHARS]) + "…"
    return first, rest


def _last_repliers(entry_ids):
    """{entry_id: username of whoever wrote the newest reply in its
    thread}: people's replies only, in one query."""
    if not entry_ids:
        return {}
    rows = (db.session.query(Node.audience_root_id, User.username)
            .join(User, User.id == Node.user_id)
            .filter(Node.audience_root_id.in_(entry_ids),
                    Node.audience_root_id != Node.id,
                    Node.privacy_level == cz.CIRCLES,
                    Node.deleted_at.is_(None),
                    Node.node_type != "llm")
            .order_by(Node.created_at.desc(), Node.id.desc())
            .limit(500).all())
    out = {}
    for entry_id, username in rows:
        out.setdefault(entry_id, username)
    return out


@circles_bp.route("/shared-with-me", methods=["GET"])
@login_required
def shared_with_me():
    """Entries shared with the current user, newest first, grouped by
    person on the page. Reading this records nothing: no read state, no
    counts. ``?before=<entry id>`` continues below that entry."""
    from backend.utils.encryption import prefetch_deks
    query = Node.query.filter(
        Node.id.in_(readable_shared_entries(current_user.id)))
    before = request.args.get("before", type=int)
    if before is not None:
        anchor = db.session.get(Node, before)
        if anchor is not None:
            query = query.filter(db.or_(
                Node.created_at < anchor.created_at,
                db.and_(Node.created_at == anchor.created_at,
                        Node.id < anchor.id)))
    rows = query.order_by(Node.created_at.desc(), Node.id.desc()).limit(
        SHARED_PAGE_SIZE + 1).all()
    has_more = len(rows) > SHARED_PAGE_SIZE
    rows = rows[:SHARED_PAGE_SIZE]

    owner_ids = {cz.entry_owner_id(n) for n in rows}
    names = dict(db.session.query(User.id, User.username).filter(
        User.id.in_(owner_ids))) if owner_ids else {}
    repliers = _last_repliers([n.id for n in rows])
    prefetch_deks(n.content for n in rows)

    people, by_owner = [], {}
    for n in rows:
        owner_id = cz.entry_owner_id(n)
        group = by_owner.get(owner_id)
        if group is None:
            group = by_owner[owner_id] = {
                "username": names.get(owner_id), "entries": []}
            people.append(group)
        title, preview = _title_and_preview(n.get_content())
        replied = repliers.get(n.id)
        group["entries"].append({
            "id": n.id,
            "title": title,
            "preview": preview,
            "created_at": iso_utc(n.created_at),
            "last_reply_by": replied,
            "last_reply_by_me": replied == current_user.username,
        })

    hidden = [name for (name,) in db.session.query(User.username)
              .join(CircleMute, CircleMute.muted_user_id == User.id)
              .filter(CircleMute.user_id == current_user.id,
                      User.deleted_at.is_(None))
              .order_by(func.lower(User.username))]
    return jsonify({
        "people": people,
        "hidden": hidden,
        "next_before": rows[-1].id if rows and has_more else None,
    }), 200


@circles_bp.route("/mutes", methods=["POST"])
@login_required
def add_mute():
    """"Don't show <name>'s entries". Only a person who shares (or
    shared) something with the user can be hidden, found among those
    people, so this is no way to test whether a name exists."""
    name = cz.clean_name((request.get_json(silent=True) or {}).get("username"))
    sender = User.query.filter(
        func.lower(User.username) == name.lower()).first() if name else None
    if sender is not None and sender.id != current_user.id:
        shares = db.session.query(Node.id).filter(
            Node.id.in_(readable_shared_entries(current_user.id)),
            func.coalesce(Node.human_owner_id, Node.user_id) == sender.id,
        ).first() is not None
        noted = db.session.query(cz.UserNotification.id).filter_by(
            user_id=current_user.id, type=cz.NOTE_TYPE,
            actor_user_id=sender.id).first() is not None
        if shares or noted:
            cz.mute(current_user.id, sender)
            db.session.commit()
            return jsonify({"hidden": sender.username}), 200
    return jsonify({"error": "Not found"}), 404


@circles_bp.route("/mutes/<username>", methods=["DELETE"])
@login_required
def remove_mute(username):
    row = (db.session.query(CircleMute)
           .join(User, User.id == CircleMute.muted_user_id)
           .filter(CircleMute.user_id == current_user.id,
                   func.lower(User.username) == cz.clean_name(username).lower())
           .first())
    if row is None:
        return jsonify({"error": "Not found"}), 404
    cz.unmute(current_user.id, row.muted_user_id)
    db.session.commit()
    return jsonify({"shown": True}), 200
