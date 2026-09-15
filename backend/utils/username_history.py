"""Former handles → current owner (#253).

Public permalinks carry the username, so a rename must keep the old URLs
alive: ``resolve_handle`` maps any handle, current or former, to its owner
and says which it was, and ``record_rename`` maintains the history on a
rename. Lookups are case-insensitive, matching ``validate_username``.
"""
from backend.extensions import db
from backend.models import User, UsernameHistory


def former_handle_owner(username):
    """The user who used to publish under *username*, or None. Newest
    rename wins if the same former handle was ever held by two users
    (impossible while the reservation in validate_username holds, kept
    deterministic anyway)."""
    row = (UsernameHistory.query
           .filter(db.func.lower(UsernameHistory.old_username)
                   == (username or "").lower())
           .order_by(UsernameHistory.changed_at.desc(),
                     UsernameHistory.id.desc())
           .first())
    return row.user if row else None


def resolve_handle(username):
    """(user, moved): the owner of *username* and whether it is a FORMER
    handle (moved=True → the caller should 301 to user.username). (None,
    False) when nobody holds or held it."""
    user = User.query.filter_by(username=username).first()
    if user is not None:
        return user, False
    user = former_handle_owner(username)
    return user, user is not None


def record_rename(user, old_username, new_username):
    """Keep the history straight across a rename from old → new:
    the old handle becomes a redirect (one row per former handle per
    user), and if the new handle is one of the user's OWN former handles
    it stops being a redirect because it is live again."""
    if not old_username or old_username.lower() == new_username.lower():
        return
    (UsernameHistory.query
     .filter(UsernameHistory.user_id == user.id,
             db.func.lower(UsernameHistory.old_username)
             == new_username.lower())
     .delete(synchronize_session=False))
    exists = (UsernameHistory.query
              .filter(UsernameHistory.user_id == user.id,
                      db.func.lower(UsernameHistory.old_username)
                      == old_username.lower())
              .first())
    if exists is None:
        db.session.add(UsernameHistory(user_id=user.id,
                                       old_username=old_username))
