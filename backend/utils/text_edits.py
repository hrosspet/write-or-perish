"""Exact-text edits of a document: a list of {old_text, new_text}
replacements applied in order, all or nothing.

Shared by the artifact tool (update_artifact's `edits`, resolved in
tasks/llm_completion._resolve_artifact_write) and the todo merge
(utils/todo_merge_edits.py), so both refuse the same anchors the same way.
"""


def apply_text_edits(text, edits, not_found_hint=""):
    """Apply *edits* to *text* in order. Each edit's old_text must occur
    exactly once in the text as it is at that point (after the earlier
    edits) and is replaced by its new_text.

    All or nothing: the first edit that can't be applied stops the run,
    and nothing is returned but the error, which names the edit by its
    index so the model can fix that anchor. *not_found_hint* is added to
    the "not found" error (the artifact tool points at its full-text
    mode there; the todo merge has none).

    Returns (new_text, None) on success or (None, error_message).
    """
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return None, f"edits[{i}] is not an object."
        old = edit.get("old_text") or ""
        new = edit.get("new_text")
        if not old:
            return None, (f"edits[{i}].old_text is empty — every edit "
                          "needs the exact text to replace.")
        if new is None:
            return None, f"edits[{i}].new_text is missing."
        count = text.count(old)
        if count == 0:
            return None, (
                f"edits[{i}].old_text was not found in the current "
                f"version. Match the current text exactly (whitespace "
                f"included){not_found_hint}.")
        if count > 1:
            return None, (
                f"edits[{i}].old_text matches {count} places — include "
                f"more surrounding context so it's unique.")
        text = text.replace(old, new, 1)
    return text, None
