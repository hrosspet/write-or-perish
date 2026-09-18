"""Former handles → current owner (#253).

Public permalinks carry the username, so a rename must keep the old URLs
alive: ``resolve_public_handle`` maps a handle, current or former, to the
account that shares publicly under it and says whether the URL belongs
under another spelling; ``record_rename`` maintains the history on a
rename; ``former_handle_owner`` is the reservation the username-issuing
paths check. Handles are matched case-insensitively throughout, like the
uniqueness ``validate_username`` enforces.
"""
from backend.extensions import db
from backend.models import Node, User, UsernameHistory


def _key(username):
    return (username or "").lower()


def former_handle_owner(username):
    """The account that published under *username* and renamed away, or
    None. The old URLs still redirect to it, so the handle is theirs to
    take back and nobody else's to claim."""
    row = UsernameHistory.query.filter_by(old_username=_key(username)).first()
    return row.user if row else None


def resolve_public_handle(username):
    """(user, moved) for a /@<username> URL.

    ``user`` is the account that currently shares publicly under the
    handle, or used to. ``moved`` says whether the URL belongs under
    /@<user.username> instead: ``"former"`` for a former handle,
    ``"case"`` for the current one written in another case (every page
    has one URL), False otherwise. (None, False) for a
    handle nobody holds or held AND for an account that has sharing off:
    the two must stay indistinguishable (the 404 parity the public pages
    keep), because a redirect from a former handle names the new one,
    and for an account that went private since renaming that is a leak.

    Callers redirect only where the target page renders, for the same
    reason: an old handle must not confirm what its owner has under the
    new one beyond what is public there anyway.
    """
    # Exact match first: the unique index serves it, and it is the common
    # case. Only a miss pays for the lower() scan of the (small) user table.
    user = User.query.filter_by(username=username).first()
    moved = False
    if user is None:
        user = User.query.filter(
            db.func.lower(User.username) == _key(username)).first()
        moved = "case" if user is not None else False
    if user is None:
        user = former_handle_owner(username)
        moved = "former" if user is not None else False
    if user is None or not user.public_sharing_enabled:
        return None, False
    return user, moved


def _published_under(user):
    """True when the account has public writing: a living public root,
    which is what /@<handle> renders (while sharing is on). Deliberately
    not conditioned on the sharing toggle: sharing can be paused around
    a rename, and the old URLs were out there all the same — dropping
    the reservation then would release the handle to a squatter and
    kill every link once sharing resumes. While sharing stays off, the
    redirect never fires (resolve_public_handle), so nothing is
    revealed; only the reservation persists."""
    return db.session.query(Node.id).filter(
        Node.parent_id.is_(None),
        (Node.human_owner_id == user.id) | (Node.user_id == user.id),
        Node.privacy_level == "public",
        Node.deleted_at.is_(None),
    ).first() is not None


def record_rename(user, old_username, new_username):
    """Keep the history straight across a rename from old → new.

    The new handle is live on this account now, so no redirect for it can
    stand (its own former handle taken back, normally: any other account's
    would have been refused as reserved). The old handle becomes a
    redirect — and stays reserved — only if the account has public
    writing (a living public root): a rename by an account with nothing
    published leaves nothing behind, so handles can't be reserved by
    cycling through them. A case-only change is no rename: current
    handles resolve case-insensitively and redirect to their stored
    spelling.
    """
    old, new = _key(old_username), _key(new_username)
    if not old or old == new:
        return
    (UsernameHistory.query.filter(UsernameHistory.old_username == new)
     .delete(synchronize_session=False))
    if not _published_under(user):
        return
    # The handle was live here until this request, which overrides any
    # redirect for it (none can exist while the reservation holds; this
    # keeps the unique index from turning one into a failed rename).
    (UsernameHistory.query.filter(UsernameHistory.old_username == old)
     .delete(synchronize_session=False))
    db.session.add(UsernameHistory(user_id=user.id, old_username=old))
