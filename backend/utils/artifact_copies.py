"""The artifact texts a model turn was given, and whether a write still
rests on them.

update_artifact names the version a write is based on (base_version,
#604), and a write based on a version that is no longer the latest saves
nothing. A tick or a "+" on the artifacts page edits the user's own latest
version in place (routes/artifacts.patch_artifact): its text changes, its
number does not. A write based on the text the model read before such an
edit passed the number check and put that text back, without the tick.

So a turn keeps, for each version whose text reached the model, the
version's edit stamp (UserArtifact.updated_at, which every in-place edit
sets) as that text had it: at the turn's start for what its prompt shows,
and again whenever a round shows a version's text (a read, the current
text after a refused write, the model's own write). A write is refused
when the version it is based on carries another stamp now, or when the
model was never given that version's text; the next round gets the
current text to retry on (llm_completion._artifact_current_text). A write
kept for the user's approval keeps the stamp, so approving it later saves
nothing when the version was edited in place meanwhile.

A version a session has pinned is never edited in place (a tick on it makes
a new version, which the number check catches), so this matters for the
versions read in the turn's own rounds, before their pin is committed, and
for a conversation that started with no artifact pinned. Its cached system
render (#192) can be older than the turn's start, and an in-place edit
between the two is not seen here.
"""
from backend.extensions import db
from backend.models import UserArtifact

# The payload key of a held write (utils/tool_approvals.ApprovalHold).
HELD_STAMP_KEY = "base_edited_at"


def edit_stamp(row):
    """*row*'s in-place edit stamp as text, None when it was never edited
    in place."""
    return row.updated_at.isoformat() if row.updated_at else None


class ArtifactCopies:
    """Per turn: {version id: the edit stamp of the text the model got}."""

    def __init__(self, stamps=None):
        self._stamps = dict(stamps or {})

    @classmethod
    def at_turn_start(cls, user_id):
        """The stamps of every live version of *user_id*'s artifacts now,
        before the turn's prompt is built: what its inline copies and the
        history's replayed reads show (one query, no content)."""
        rows = db.session.query(UserArtifact.id, UserArtifact.updated_at) \
            .filter(UserArtifact.user_id == user_id,
                    UserArtifact.deleted_at.is_(None)).all()
        return cls({row_id: (updated.isoformat() if updated else None)
                    for row_id, updated in rows})

    @classmethod
    def from_hold(cls, payload):
        """The copy a write kept for approval was made on, or None for a
        call kept before stamps were (its approval checks the number
        only, as before)."""
        artifact_id = (payload or {}).get("previous_artifact_id")
        if artifact_id is None or HELD_STAMP_KEY not in payload:
            return None
        return cls({artifact_id: payload[HELD_STAMP_KEY]})

    def given(self, row):
        """The model got *row*'s text as it is now."""
        if row is not None:
            self._stamps[row.id] = edit_stamp(row)

    def knows(self, row):
        """Whether the model got *row*'s text in this turn or its prompt."""
        return row.id in self._stamps

    def changed(self, row):
        """Whether *row* was edited in place since the model got its text
        (also when it never got it)."""
        return (row.id not in self._stamps
                or self._stamps[row.id] != edit_stamp(row))
