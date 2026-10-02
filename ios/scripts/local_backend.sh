#!/bin/sh
# Helpers for testing the app against the local Docker backend (`make dev`).
#
# Everything here acts on the TEST USER ONLY (user 5, "seowriter"). Other local
# users hold real personal data: never point these commands at them.
#
#   ios/scripts/local_backend.sh session-cookie   # prints a signed Flask `session` cookie value
#   ios/scripts/local_backend.sh magic-link [path] # prints a fresh 15-minute sign-in link
#                                                  # (optional landing path, e.g. /welcome)
#   ios/scripts/local_backend.sh state [modes…]   # switches test state, prints metadata
#
# State modes: terms_old (accepted terms out of date), unapproved, restore
# (approved + terms current), tz_utc, add_notification (an unread "fix_ready"
# notification linking to /log), del_notifications, craft_off, m4_cleanup (removes
# what M4ScreensUITests leave: the test user's todo and profile rows, the
# `m4-test` artifact, and nodes holding "M4 import test" or the published-then-
# revoked "M4 test share"; the backend has no
# delete route for profiles, todos or artifacts).
#
# The output is a credential for the local test user: keep it out of commits.
set -e
CONTAINER=${LOORE_BACKEND_CONTAINER:-write-or-perish-backend-1}
TEST_USER_ID=5
TEST_USERNAME=seowriter

run_py() {
  docker exec -i "$CONTAINER" sh -c 'cd /app && PYTHONPATH=/app python - 2>/dev/null' | tail -1
}

case "$1" in
  session-cookie)
    run_py <<EOF
from backend import create_app
app = create_app()
with app.app_context():
    print(app.session_interface.get_signing_serializer(app).dumps({"_user_id": "$TEST_USER_ID", "_fresh": True}))
EOF
    ;;
  magic-link)
    NEXT_PATH="${2:-}"
    run_py <<EOF
from datetime import datetime, timedelta
from backend import create_app
app = create_app()
with app.app_context():
    from backend.extensions import db
    from backend.models import User
    from backend.utils.magic_link import generate_magic_link_token, hash_token
    u = db.session.get(User, $TEST_USER_ID)
    assert u.username == "$TEST_USERNAME"
    token = generate_magic_link_token(u.email, "$NEXT_PATH" or None)
    u.magic_link_token_hash = hash_token(token)
    u.magic_link_expires_at = datetime.utcnow() + timedelta(minutes=15)
    db.session.commit()
    print("http://localhost:5010/auth/magic-link/verify?token=" + token)
EOF
    ;;
  state)
    shift
    MODES="$*"
    run_py <<EOF
from backend import create_app
app = create_app()
with app.app_context():
    from backend.extensions import db
    from backend.models import User, UserNotification, UserTodo, UserProfile, UserArtifact, Node
    u = db.session.get(User, $TEST_USER_ID)
    assert u.username == "$TEST_USERNAME"
    for mode in "$MODES".split():
        if mode == "terms_old":
            u.accepted_terms_version = "1.0"
        elif mode == "unapproved":
            u.approved = False
        elif mode == "restore":
            u.approved = True
            u.accepted_terms_version = "2.0"
        elif mode == "tz_utc":
            u.timezone = "UTC"
        elif mode == "add_notification":
            db.session.add(UserNotification(user_id=$TEST_USER_ID, type="fix_ready", title="Export works again",
                                            body="Thanks for the report. The fix is live.", link="/log"))
        elif mode == "del_notifications":
            UserNotification.query.filter_by(user_id=$TEST_USER_ID, title="Export works again").delete()
        elif mode == "craft_off":
            u.craft_mode = False
        elif mode == "m4_cleanup":
            rows = (UserTodo.query.filter_by(user_id=$TEST_USER_ID).all()
                    + UserProfile.query.filter_by(user_id=$TEST_USER_ID).all()
                    + UserArtifact.query.filter_by(user_id=$TEST_USER_ID, kind="m4-test").all()
                    + [n for n in Node.query.filter_by(user_id=$TEST_USER_ID).all()
                       if "M4 import test" in (n.get_content() or "")
                       or "M4 test share" in (n.get_content() or "")])
            for row in rows:
                db.session.delete(row)
            print("m4_cleanup removed %d rows" % len(rows))
        else:
            raise SystemExit("unknown mode: " + mode)
    db.session.commit()
    n = UserNotification.query.filter_by(user_id=$TEST_USER_ID, status="unread").count()
    print("user %s approved=%s terms=%s tz=%s craft=%s unread_notifications=%d" % (
        u.username, u.approved, u.accepted_terms_version, u.timezone, u.craft_mode, n))
EOF
    ;;
  *)
    sed -n '2,19p' "$0"
    exit 1
    ;;
esac
