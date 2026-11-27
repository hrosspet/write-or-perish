"""Circles routes: the owner's circles, sharing previews, "Shared with
you", the per-reader mute and a reader leaving (dark behind CIRCLES_V1 /
CIRCLES_USER_IDS; every route answers 404 while Circles is off for the
user).

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
from backend.utils.privacy import readable_entry_ids
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
    out = []
    for c in circles:
        row = {
            "id": c.id,
            "name": c.name,
            "members": [{"username": u.username,
                         "only_new": m.entries_from is not None}
                        for m, u in members[c.id]],
            "shared_count": counts.get(c.id, 0),
        }
        # The first cohort (COHORT_V1 only): the circle Loore keeps for
        # the group, which its owner can't rename, delete or edit.
        cohort = _cohort_of(c)
        if cohort is not None:
            row["cohort"] = {"name": cohort.name, "slug": cohort.slug}
        out.append(row)
    return out


def _cohort_of(circle):
    if not getattr(circle, "cohort_id", None):
        return None
    from backend.utils.cohort import cohort_of_circle
    return cohort_of_circle(circle)


def _kept_by_loore(circle):
    """409 for a hand edit of a circle Loore keeps for a cohort (its name,
    its deletion, its people), else None."""
    cohort = _cohort_of(circle)
    if cohort is None:
        return None
    from backend.utils.cohort import cohort_circle_text
    return jsonify({"error": cohort_circle_text(cohort),
                    "code": "cohort_circle"}), 409


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
    kept = _kept_by_loore(circle)
    if kept:
        return kept
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
    kept = _kept_by_loore(circle)
    if kept:
        return kept
    cz.delete_circle(circle)
    db.session.commit()
    return jsonify({"deleted": True}), 200


# ── People ──────────────────────────────────────────────────────────────

def _lookup(username):
    """(person, error response) for one typed name. A name nobody has is
    counted before the answer says so (circles.record_lookup_miss)."""
    def limited():
        return (jsonify({"error": cz.LOOKUP_LIMIT,
                         "code": "circle_lookup_limit"}), 429)
    if cz.lookup_blocked(current_user.id):
        return None, limited()
    person = cz.find_person(username, current_user.id)
    if person is None:
        if cz.over_lookup_limit(cz.record_lookup_miss(current_user.id)):
            return None, limited()
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
    kept = _kept_by_loore(circle)
    if kept:
        return kept
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
    kept = _kept_by_loore(circle)
    if kept:
        return kept
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
    cz.add_circle_member(circle, person, data["include_existing"],
                         now=datetime.utcnow())
    db.session.commit()
    return jsonify({"circle": _serialize_circles([circle])[0]}), 201


def _name_in_body():
    """The Loore name a request names another person by: in its JSON
    body, never in its address. The address is what the account's
    last-seen place keeps (activity.touch_last_seen) and what error
    reports and log lines carry."""
    name = (request.get_json(silent=True) or {}).get("username")
    return cz.clean_name(name if isinstance(name, str) else "")


@circles_bp.route("/<int:circle_id>/members", methods=["DELETE"])
@login_required
def remove_member(circle_id):
    """Remove a person, named by ``username`` in the body: the circle's
    entries are closed to them from this moment. Found among the
    circle's members only, so a removed account that can no longer be
    looked up can still be removed."""
    circle = cz.own_circle(circle_id, current_user.id)
    if circle is None:
        return jsonify({"error": "Not found"}), 404
    kept = _kept_by_loore(circle)
    if kept:
        return kept
    username = _name_in_body()
    if not username:
        return jsonify({"error": "Not found"}), 404
    row = (db.session.query(CircleMember)
           .join(User, User.id == CircleMember.user_id)
           .filter(CircleMember.circle_id == circle.id,
                   func.lower(User.username) == username.lower())
           .first())
    if row is None:
        return jsonify({"error": "Not found"}), 404
    cz.remove_circle_member(row)
    db.session.commit()
    return jsonify({"circle": _serialize_circles([circle])[0]}), 200


@circles_bp.route("/audience-preview", methods=["POST"])
@login_required
def audience_preview():
    """Who would read an entry shared with these circles and names: the
    sentence the form shows before saving. ``node_id``: the user's own
    entry being edited, whose circles keep the time they were shared
    with it (a member added since with "Only new entries" is not named);
    without it, a new entry."""
    data = request.get_json(silent=True) or {}
    try:
        circles, people = cz.resolve_audience(
            current_user, data.get("circle_ids"), data.get("usernames"))
    except cz.AudienceError as e:
        return e.response()
    entry = None
    try:
        node_id = int(data["node_id"]) if data.get("node_id") else None
    except (TypeError, ValueError):
        node_id = None
    if node_id is not None:
        node = db.session.get(Node, node_id)
        if node is not None and cz.entry_owner_id(node) == current_user.id:
            entry = node
    readers = cz.preview_readers(circles, people, current_user.id,
                                 entry=entry)
    # ``content``: the text about to be shared. It may not quote a node
    # one of these people cannot read (Peter, 2026-11-12): the same 409
    # the save gives, before anything is saved, so the form shows the
    # dialog naming the quoted entry. For an entry already shared, a
    # quote it had is checked against the people it gains only. With
    # ``share_path``, the discussion leading up to the entry (the edited
    # entry's, or for a new entry the one from ``parent_id`` up) is
    # checked the same way.
    content = data.get("content")
    if readers and ((isinstance(content, str) and content)
                    or data.get("share_path")):
        found = _quotes_unreadable_by(
            readers, content if isinstance(content, str) else "", entry,
            data)
        if found:
            from backend.utils.publish_quotes import quotes_not_public_response
            return quotes_not_public_response(found, current_user.id,
                                              shared=True)
    return jsonify({"readers": readers}), 200


def _quotes_unreadable_by(readers, content, entry, data):
    """For audience_preview: the quoted nodes that the people named
    *readers* (usernames) and the user cannot all read, in *content* and,
    with ``share_path``, in the discussion that would be shared with the
    entry (publish_quotes.unpublished_quotes, mute-free reader lists). A
    quote an already shared node had is checked against the people it
    gains only."""
    from backend.utils.publish_quotes import circle_readers, unpublished_quotes
    from backend.utils.quotes import find_quote_ids
    ids = {u.id for u in User.query.filter(User.username.in_(readers))}
    ids.add(current_user.id)
    shared_now = entry is not None and cz.is_shared_entry(entry)
    before = circle_readers(entry) if shared_now else set()
    added = ids - before
    checks = [(content, set(find_quote_ids(entry.get_content() or ""))
               if shared_now else set())]
    if data.get("share_path"):
        if entry is not None:
            path = cz.thread_path(entry, current_user.id)
        else:
            path = []
            try:
                parent_id = int(data.get("parent_id") or 0)
            except (TypeError, ValueError):
                parent_id = 0
            current = db.session.get(Node, parent_id) if parent_id else None
            for _ in range(cz.THREAD_PATH_HOPS):
                if (current is None or cz.entry_owner_id(current)
                        != current_user.id):
                    break
                path.append(current)
                current = current.parent
        # The path's keys are unwrapped together first, so the reads below
        # make no key call one after another.
        from backend.utils.encryption import prefetch_deks
        prefetch_deks(above.content for above in path
                      if above.deleted_at is None
                      and above.privacy_level != "public")
        for above in path:
            if above.deleted_at is not None or above.privacy_level == "public":
                continue
            text = above.get_content() or ""
            already = (shared_now and above.privacy_level == "circles"
                       and above.audience_root_id == entry.id)
            checks.append((text, set(find_quote_ids(text)) if already
                           else set()))
    found, seen = [], set()
    for text, old_ids in checks:
        nodes = unpublished_quotes([text], current_user.id,
                                   skip_ids=old_ids, readers=ids)
        if added and old_ids:
            nodes += unpublished_quotes([text], current_user.id,
                                        readers=added)
        for n in nodes:
            if n.id not in seen:
                seen.add(n.id)
                found.append(n)
    return found


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
                    # Written after the entry: the discussion leading up
                    # to an entry may point at it too, and is no reply.
                    Node.id > Node.audience_root_id,
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
    counts. ``?before=<entry id>`` continues below that entry; an id
    that is not an entry shared with the caller is ignored, like one
    that does not exist."""
    from backend.utils.encryption import prefetch_deks
    readable = readable_entry_ids(current_user.id)
    query = Node.query.filter(Node.id.in_(readable))
    before = request.args.get("before", type=int)
    if before is not None:
        anchor = (db.session.get(Node, before)
                  if before in set(readable) else None)
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

    hidden_rows = (db.session.query(User.id, User.username)
                   .join(CircleMute, CircleMute.muted_user_id == User.id)
                   .filter(CircleMute.user_id == current_user.id,
                           User.deleted_at.is_(None))
                   .order_by(func.lower(User.username)).all())
    # The people the user hid whose circles or entries still hold them:
    # their entries cannot be opened to leave from, so the list offers it.
    leavable = cz.leavable_authors(current_user.id,
                                   [uid for uid, _ in hidden_rows])
    return jsonify({
        "people": people,
        "hidden": [name for _, name in hidden_rows],
        "hidden_leavable": [name for uid, name in hidden_rows
                            if uid in leavable],
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
            Node.id.in_(readable_entry_ids(current_user.id)),
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


def _leave_target():
    """(author, entry or None, plan) for a leave request, or None when
    there is nothing for the user to leave there. ``entry_id``: a shared
    entry the user can open now (the read routes' own check) and whose
    audience holds them (leaving from its page). ``username`` (in the
    body): a person the user hid whose circles or entries hold them (the
    list of hidden people offers it, because their entries cannot be
    opened). Any other request gets the answer of one that names
    nothing, so this is no way to test a name or an entry."""
    from backend.utils.privacy import can_user_access_node
    data = request.get_json(silent=True) or {}
    entry = None
    if data.get("entry_id") is not None:
        try:
            entry = db.session.get(Node, int(data["entry_id"]))
        except (TypeError, ValueError):
            return None
        if (entry is None or entry.deleted_at is not None
                or not cz.is_shared_entry(entry)
                or cz.entry_owner_id(entry) == current_user.id
                or not can_user_access_node(entry, current_user.id)
                or current_user.id not in cz.reader_ids(entry.id)):
            return None
        author = db.session.get(User, cz.entry_owner_id(entry))
    else:
        name = _name_in_body()
        if not name:
            return None
        author = (db.session.query(User)
                  .join(CircleMute, CircleMute.muted_user_id == User.id)
                  .filter(CircleMute.user_id == current_user.id,
                          func.lower(User.username) == name.lower())
                  .first())
    if author is None or author.id == current_user.id:
        return None
    plan = cz.leave_plan(current_user.id, author.id, entry=entry)
    if not plan:
        return None
    return author, entry, plan


def _leave_answer(author, plan):
    return {"author": author.username, "closes": plan.closes}


def _cohort_leave_refusal(plan):
    """409 when leaving would take the user out of a circle Loore keeps
    for a cohort they are a member of (COHORT_V1 only): the group is left
    by telling its founder, and a person's entries are hidden with the
    mute."""
    if not plan.member_ids:
        return None
    from backend.utils.cohort import cohort_enabled, cohort_leave_text
    if not cohort_enabled():
        return None
    circle = (Circle.query.join(CircleMember,
                                CircleMember.circle_id == Circle.id)
              .filter(CircleMember.id.in_(plan.member_ids),
                      Circle.cohort_id.isnot(None)).first())
    cohort = _cohort_of(circle) if circle is not None else None
    if cohort is None:
        return None
    return jsonify({"error": cohort_leave_text(cohort),
                    "code": "cohort_circle"}), 409


@circles_bp.route("/leave/preview", methods=["POST"])
@login_required
def leave_preview():
    """What leaving would do, for the dialog: whose audience, and how many
    of their entries close to the user with it. Changes nothing."""
    target = _leave_target()
    if target is None:
        return jsonify({"error": "Not found"}), 404
    author, _entry, plan = target
    refused = _cohort_leave_refusal(plan)
    if refused:
        return refused
    return jsonify(_leave_answer(author, plan)), 200


@circles_bp.route("/leave", methods=["POST"])
@login_required
def leave():
    """The user leaves: ``entry_id``, the circles through which they read
    that entry and their name on it; ``username``, everything of that
    hidden person's that holds them. They then get the answers a removed
    reader gets, and nobody sees their name among the readers any more."""
    target = _leave_target()
    if target is None:
        return jsonify({"error": "Not found"}), 404
    author, _entry, plan = target
    refused = _cohort_leave_refusal(plan)
    if refused:
        return refused
    cz.leave(plan)
    db.session.commit()
    return jsonify({"left": True, **_leave_answer(author, plan)}), 200


@circles_bp.route("/mutes", methods=["DELETE"])
@login_required
def remove_mute():
    """Show a person's entries again, named by ``username`` in the body
    (_name_in_body), among the people the user hid."""
    username = _name_in_body()
    if not username:
        return jsonify({"error": "Not found"}), 404
    row = (db.session.query(CircleMute)
           .join(User, User.id == CircleMute.muted_user_id)
           .filter(CircleMute.user_id == current_user.id,
                   func.lower(User.username) == username.lower())
           .first())
    if row is None:
        return jsonify({"error": "Not found"}), 404
    cz.unmute(current_user.id, row.muted_user_id)
    db.session.commit()
    return jsonify({"shown": True}), 200
