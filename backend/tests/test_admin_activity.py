"""Admin Activity tab: engagement since activation measured directly
(writes / asks / voice per day, last seen, retention), the throttled
last-seen touch, and approved_at stamping on activation."""
from datetime import datetime, timedelta

from backend.tests.test_admin_access import app, users, _login, _db  # noqa: F401
from backend.models import User, Node, APICostLog, UserProfile
from backend.utils import activity as act


def _node(user, when, origin=None, audio=None, node_type="user", author=None):
    n = Node(user_id=(author or user).id, human_owner_id=user.id, node_type=node_type, origin=origin,
             privacy_level="private", ai_usage="chat", token_count=1,
             audio_original_url=audio, created_at=when)
    n.set_content("x")
    _db.session.add(n)
    return n


def _cost(user, when, rtype, micro=1000, audio=None):
    _db.session.add(APICostLog(user_id=user.id, model_id="m", request_type=rtype,
                               input_tokens=0, output_tokens=0, cost_microdollars=micro,
                               audio_duration_seconds=audio, created_at=when))


def test_touch_last_seen_throttles_and_tracks_area(app):  # noqa: F811
    u = User(username="t", approved=True)
    _db.session.add(u); _db.session.commit()
    t0 = datetime(2026, 8, 29, 10, 0, 0)
    # App bootstrap fires first (polling path): time written, area stays unknown.
    assert act.touch_last_seen(u, "/api/dashboard", now=t0) is True
    assert (u.last_seen_at, u.last_seen_path) == (t0, None)
    # Profile request 200ms later, inside the interval: a NEW area always lands.
    assert act.touch_last_seen(u, "/api/profile/versions", now=t0 + timedelta(seconds=1)) is True
    assert u.last_seen_path == "/api/profile/versions"
    # Same area again inside the interval: throttled, no write.
    assert act.touch_last_seen(u, "/api/profile/versions", now=t0 + timedelta(minutes=2)) is False
    assert u.last_seen_at == t0 + timedelta(seconds=1)
    # Polls inside the interval: throttled; after it: time moves, area kept.
    assert act.touch_last_seen(u, "/api/notifications", now=t0 + timedelta(minutes=3)) is False
    t1 = t0 + timedelta(minutes=7)
    assert act.touch_last_seen(u, "/api/dashboard", now=t1) is True
    assert (u.last_seen_at, u.last_seen_path) == (t1, "/api/profile/versions")
    # Moving to the editor: written immediately even inside the interval.
    assert act.touch_last_seen(u, "/api/nodes/1", now=t1 + timedelta(seconds=5)) is True
    assert u.last_seen_path == "/api/nodes/1"


def test_activity_report_counts_since_activation(app):  # noqa: F811
    now = datetime(2026, 8, 29, 12, 0, 0)
    act_at = now - timedelta(days=3)
    u = User(username="seeded", approved=True, approved_at=act_at, prefilled_handle="seeded",
             accepted_terms_at=act_at + timedelta(hours=1), last_seen_at=now - timedelta(hours=2),
             last_seen_path="/api/profile/versions")
    quiet = User(username="quiet", approved=True, approved_at=act_at)
    inactive = User(username="inactive", approved=False)
    spam = User(username="spammy", approved=True, spam=True)
    llm = User(username="llm-m", approved=True)  # placeholder that authors LLM nodes
    _db.session.add_all([u, quiet, inactive, spam, llm]); _db.session.flush()
    # Pre-fill (before activation) must not count; X pre-fill cost row marks the seed source.
    _node(u, act_at - timedelta(days=1), origin="twitter")
    _cost(u, act_at - timedelta(days=1), "x_prefill", micro=16_010_000)
    _cost(u, act_at - timedelta(days=1), "profile_batch", micro=500_000)
    # Day 1 after activation: two writes (one voice) + one ask + transcription.
    d1 = act_at + timedelta(days=1, hours=2)
    _node(u, d1); _node(u, d1 + timedelta(minutes=5), audio="a.webm")
    _cost(u, d1, "conversation", micro=20_000)
    _cost(u, d1, "transcription", micro=3_000, audio=90)
    # Day 3: an ask only; an LLM node (not a write); an embedding (automation).
    d3 = act_at + timedelta(days=3, minutes=1)
    _cost(u, d3, "conversation", micro=10_000)
    _node(u, d3, node_type="llm", author=llm)
    _cost(u, d3, "embedding", micro=999_000)
    # Own archive import after activation + an organic profile version.
    _node(u, d1, origin="chatgpt")
    p = UserProfile(user_id=u.id, generated_by="m", tokens_used=0, generation_type="initial",
                    created_at=d1 + timedelta(hours=1))
    p.set_content("p"); _db.session.add(p)
    _db.session.commit()

    rep = act.activity_report(days=7, now=now)
    names = [r["username"] for r in rep["users"]]
    assert "inactive" not in names and "spammy" not in names
    assert names[0] == "seeded"  # most recently seen first
    r = rep["users"][0]
    assert r["seeded"] == "x" and r["activated_at"] == act_at.isoformat()
    assert (r["writes"], r["asks"], r["voice_nodes"], r["voice_minutes"]) == (2, 2, 1, 1.5)
    assert r["active_days"] == 2 and r["days_since_activation"] == 3
    assert r["imports"] == 1 and r["profile_versions_since_activation"] == 1
    assert r["user_spend_usd"] == (20_000 + 3_000 + 10_000) / 1e6  # no prefill/batch/embedding
    assert r["day1_return"] is True and r["day7_return"] is True
    assert r["last_seen_path"] == "/api/profile/versions"
    assert len(r["strip"]) == 7 and rep["days"][-1] == now.date().isoformat()
    by_day = {c["d"]: c for c in r["strip"]}
    assert by_day[d1.date().isoformat()] == {"d": d1.date().isoformat(), "w": 2, "a": 1, "v": 1, "pre": False}
    assert by_day[(act_at - timedelta(days=1)).date().isoformat()]["pre"] is True
    q = next(x for x in rep["users"] if x["username"] == "quiet")
    assert q["seeded"] is None and q["writes"] == 0 and q["last_seen_at"] is None
    assert q["day1_return"] is False and q["active_days"] == 0


def test_activity_endpoint_and_approved_at_stamping(app, users):  # noqa: F811
    admin = users["renamed_admin"]
    target = User(username="newbie", approved=False)
    _db.session.add(target); _db.session.commit()
    client = app.test_client()
    _login(client, admin.id)
    assert client.post(f"/api/admin/users/{target.id}/toggle").status_code == 200
    fresh = User.query.get(target.id)
    assert fresh.approved is True and fresh.approved_at is not None
    r = client.get("/api/admin/activity?days=7")
    assert r.status_code == 200 and len(r.json["days"]) == 7
    assert any(u["username"] == "newbie" for u in r.json["users"])
    assert client.get("/api/admin/activity?days=999").json["days"].__len__() == 60
    # Deactivating leaves approved_at (audit) and drops the row.
    client.post(f"/api/admin/users/{target.id}/toggle")
    assert not any(u["username"] == "newbie" for u in client.get("/api/admin/activity").json["users"])
