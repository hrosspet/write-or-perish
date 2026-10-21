"""Who wrote the current intentions version (#412).

An intentions version has one of three kinds of writer:

* an inference run, which builds the intentions from scratch from the
  user's writing: the admin "Infer intentions" task (tasks/intentions.py)
  and backend/scripts/backfill_intentions.py. They are the only writers
  that record the LLM tokens the version cost (tokens_used > 0), and a
  dump carries tokens_used, so a version loaded by load_user.py keeps its
  origin;
* the user, saving or restoring a version on the artifacts page
  (generated_by "user", "manual" or "revert", tokens_used 0);
* a conversation, through the update_artifact tool (generated_by the
  reply's model or "agentic_session", tokens_used 0).

The user's edit wins (Peter, 2026-10-02): an inference run does not
replace a version the user or a conversation wrote. The admin button asks
first and replaces only the version the admin confirmed; the paths with no
dialog (the batch poller, the backfill script, load_user.py) skip.

Reads metadata only; nothing is decrypted.
"""
from backend.models import UserArtifact

KIND = "intentions"

# The cost row of an inference run whose result was not saved because the
# user's version is current (admin column "user's version kept").
KEPT_USER_EDIT_REF = "kept:user_edit"


def is_inferred(version):
    """Whether an inference run wrote this intentions version."""
    return (version.tokens_used or 0) > 0


def protected_version(user_id):
    """The user's current intentions version when an inference run must
    not replace it unasked: one the user or a conversation wrote. None
    when the user has no intentions yet or the current version is an
    inference run's."""
    current = UserArtifact.latest_for(user_id, KIND)
    if current is None or is_inferred(current):
        return None
    return current


def may_replace(user_id, replace_version_id=None):
    """Whether an inference result may become the user's current version:
    the current one is an inference run's, or it is the version an admin
    confirmed replacing (``replace_version_id``). A version the user saves
    after that confirmation is protected again."""
    kept = protected_version(user_id)
    return kept is None or kept.id == replace_version_id


def changed_by(version):
    """How the user's version came about, for the admin dialog: "user" (an
    edit), "revert" (an earlier version restored) or "conversation"."""
    if version.generated_by in ("user", "manual"):
        return "user"
    if version.generated_by == "revert":
        return "revert"
    return "conversation"
