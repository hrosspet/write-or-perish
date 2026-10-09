"""The todo merge as edits (#234).

A todo merge applies an accepted todo proposal to the user's full list
(tasks/voice_todo_merge.py). The model used to write the whole list out
again (~140 items, ~4.5k output tokens), and every copy could go wrong: an
open task ticked that the proposal never named, one digit changed in a
link, an existing item reworded, a repetition loop until the output cap.
Now the model replies with edits in the artifact tool's format, a list of
{old_text, new_text} exact-text replacements, and code applies them to the
newest list with the same helper as update_artifact (utils/text_edits.py).

The reply is a JSON object (TODO_EDITS_SCHEMA), asked for with the
provider's structured output, so it parses on every Anthropic and OpenAI
model this app calls, and a retry is a plain text turn (the reply, then
the reason it was refused).

A reply is refused, and the model gets one retry with the reason, when:
* it is not that JSON object;
* an edit's old_text is not found in the list, or found more than once
  (all or nothing, as for artifacts);
* it writes the whole list (updated_content) although the list has tasks:
  only an empty list, or one with headings and no tasks yet (the Todo
  page's Create template), has nothing to anchor edits on;
* after the edits, a line of the previous list is gone or changed, other
  than a checkbox going from `[ ]` to `[x]` (lines_not_kept);
* after the edits, an existing sub-item sits under a line the edits added:
  new lines went between the sub-items of an existing task
  (lines_moved_under_new).
If the retry is refused too, the merge fails and nothing is saved. A reply
cut off at the output cap fails at once (#432).

run_todo_merge is the one place that calls the model, applies the reply
and counts what happened; the task and the comparison script
(scripts/compare_todo_merge_models.py --current-prompt) both use it.
"""
import json
import re
from collections import Counter
from difflib import SequenceMatcher

from backend.utils.text_edits import apply_text_edits

# A list item (`- `, `* `, `+ `, `1. `), with the text after its checkbox,
# if it has one, in group 1.
_LIST_ITEM_RE = re.compile(
    r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\](?=[ \t]|$))?(.*)$")
# An unticked checkbox item: the part before `[ ]` in group 1, after in 2.
_UNTICKED_RE = re.compile(r"^([ \t]*(?:[-*+]|\d+[.)])[ \t]+)\[ \](.*)$")
# A ticked checkbox item: the part before `[x]` in group 1, after in 2.
_TICKED_RE = re.compile(r"^([ \t]*(?:[-*+]|\d+[.)])[ \t]+)\[[xX]\](.*)$")
# A markdown heading; no line below it sits under a line above it.
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")

# Heuristic: one retry after a refused reply, as the artifact tool gets one
# round to fix an anchor. A second refusal fails the merge (nothing saved).
MAX_MERGE_ATTEMPTS = 2
# Heuristic: a kept-lines refusal names at most this many lines to the
# model; a reply that broke more is wrong as a whole, and the count says so.
MAX_LINES_NAMED = 10

# Why a merge saved nothing (MergeRun.failure).
FAILURE_EMPTY = "empty"
FAILURE_TRUNCATED = "truncated"
FAILURE_FORMAT = "format"
FAILURE_ANCHOR = "anchor"
FAILURE_REWRITE = "rewrite_refused"
FAILURE_KEPT_LINES = "kept_lines"
FAILURE_SUB_ITEMS_MOVED = "sub_items_moved"

SCHEMA_NAME = "todo_edits"
# The reply format. Same edit shape as update_artifact's `edits`; strict
# structured output (OpenAI) needs every property required and no others.
TODO_EDITS_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "description": (
                "Exact-text replacements applied in order to the current "
                "todo list. Each old_text must match the list exactly "
                "(whitespace included) and occur in it exactly once; if "
                "any edit fails, none is applied. Tick an item by "
                "replacing its whole line with the same line ticked; add "
                "items by replacing the line they go after with that same "
                "line, a newline and the new lines."),
            "items": {
                "type": "object",
                "properties": {
                    "old_text": {
                        "type": "string",
                        "description": (
                            "Exact text to replace (must occur exactly "
                            "once in the current list)."),
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                },
                "required": ["old_text", "new_text"],
                "additionalProperties": False,
            },
        },
        "updated_content": {
            "type": "string",
            "description": (
                "Leave empty. Only when the todo list is empty or has no "
                "tasks yet: the complete new list, with no edits."),
        },
    },
    "required": ["edits", "updated_content"],
    "additionalProperties": False,
}

# How to write the reply. It is the parser's contract, so it lives here,
# not in the user-editable orient_apply_todo prompt (which keeps the rules
# of what a merge changes): tasks/voice_todo_merge.build_merge_messages
# appends it after the merge prompt, the file default and a prompt the
# user saved alike. A prompt saved before #234 ends with "Return ONLY the
# complete updated todo list", hence the first line.
REPLY_FORMAT = (
    "How to write your reply (this sets the form of the reply and "
    "replaces any instruction above to return the whole list):\n"
    "- Do not write the list out again: reply with edits, exact-text "
    "replacements that are applied to the current list\n"
    '- Reply with a JSON object: {"edits": [{"old_text": "...", '
    '"new_text": "..."}], "updated_content": ""}\n'
    "- old_text is text copied exactly from the current list (whitespace "
    "included) that occurs in it exactly once; new_text replaces it. "
    "Edits are applied in order, each to the list as the earlier edits "
    "left it. If one edit can't be applied, none is\n"
    "- To tick an item, old_text is its whole line and new_text the same "
    "line with `[ ]` changed to `[x]`\n"
    "- To add items, old_text is the whole line they go after and "
    "new_text is that same line, a newline, and the new lines. A new "
    "top-level task goes after the last sub-item of the task above it, "
    "never between the sub-items of an existing task. A new "
    "section (`## Name` and its items) goes after the last line of the "
    "section it follows, or after the last line of the list\n"
    "- Copy the line you use as old_text into new_text character for "
    "character. Every line of the current list must still be there after "
    "your edits, unchanged except for `[ ]` becoming `[x]`; edits that "
    "change or drop a line are refused\n"
    "- Leave updated_content empty. Only when the todo list is empty or "
    "has no tasks yet, put the complete new list in updated_content and "
    "send no edits\n"
    "- Return ONLY the JSON object — no commentary")

FORMAT_ERROR = ("The reply was not a JSON object with `edits` (a list of "
                "{old_text, new_text}) and `updated_content` (a string).")
REWRITE_REFUSED_ERROR = (
    "updated_content must be empty: the todo list already has tasks, so "
    "the changes go in edits. A full copy of the list is not accepted.")
RETRY_MESSAGE = (
    "Your reply could not be applied, so nothing was changed: {error}"
    "\n\nSend the whole reply again, corrected, as the same JSON object. "
    "Its edits are applied to the same current todo list as before.")


def has_tasks(todo_text):
    """Whether the list has an item with text: `- [ ] call mom`,
    `- [x] call mom` or `- call mom`. The Todo page's Create template
    (headings and empty `- [ ] ` lines) has none."""
    for line in (todo_text or "").splitlines():
        item = _LIST_ITEM_RE.match(line)
        if item and item.group(1).strip():
            return True
    return False


class MergeRun:
    """One merge: every call's response (each is billed and logged), the
    replies, and counts of what happened. ``merged`` is the list to save;
    when ``failure`` is set (a FAILURE_* value) nothing may be saved."""

    def __init__(self):
        self.responses = []
        self.replies = []
        self.edits_applied = 0
        self.full_write = False
        self.retries = 0
        self.format_errors = 0
        self.anchor_errors = 0
        self.rewrite_refusals = 0
        self.kept_lines_failures = 0
        self.sub_items_moved_failures = 0
        self.merged = None
        # The applied reply's edits (None for a full write): applied again
        # to the newest list when it changed during the merge (#477).
        self.edits = None
        self.failure = None
        # Each refused reply's kind and reason, as sent to the model; the
        # last reason is also in ``error``. A reason can quote lines of
        # the list: never log it.
        self.refusals = []
        self.error = None

    def stats(self):
        """Counts only (no list text), for logs and the script's output."""
        return {
            "calls": len(self.responses),
            "retries": self.retries,
            "edits_applied": self.edits_applied,
            "full_write": self.full_write,
            "format_errors": self.format_errors,
            "anchor_errors": self.anchor_errors,
            "rewrite_refusals": self.rewrite_refusals,
            "kept_lines_failures": self.kept_lines_failures,
            "sub_items_moved_failures": self.sub_items_moved_failures,
            "failure": self.failure,
        }


def parse_merge_reply(content):
    """(edits, updated_content, None) from the model's reply, or
    (None, None, error)."""
    text = (content or "").strip()
    # Structured output returns the bare object; a fenced one is accepted.
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*[ \t]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.rstrip())
    try:
        data = json.loads(text)
    except ValueError:
        return None, None, FORMAT_ERROR
    if not isinstance(data, dict):
        return None, None, FORMAT_ERROR
    edits = data.get("edits") or []
    updated = data.get("updated_content") or ""
    if not isinstance(edits, list) or not isinstance(updated, str):
        return None, None, FORMAT_ERROR
    # A missing or empty field is apply_text_edits' to name; a value of
    # another type is not a reply in this format.
    for edit in edits:
        if isinstance(edit, dict) and any(
                edit.get(key) is not None
                and not isinstance(edit.get(key), str)
                for key in ("old_text", "new_text")):
            return None, None, FORMAT_ERROR
    return edits, updated, None


def _compared(line):
    """Whether the kept-lines check compares *line*: not a blank line and
    not an empty list item (the Create template's `- [ ] ` lines, which a
    merge may fill or drop)."""
    if not line.strip():
        return False
    item = _LIST_ITEM_RE.match(line)
    return not (item and not item.group(1).strip())


def lines_not_kept(previous, merged):
    """The structural check: the lines of *previous* that *merged* no
    longer has, as [(line number in previous, line)].

    Every line of the previous list must still be in the merged list with
    the same text (trailing whitespace aside); the only change allowed to
    one is its checkbox going from `[ ]` to `[x]`. New lines may be added
    anywhere, and order is not checked. Blank lines and empty list items
    are not compared.

    Why: an edit inserts by replacing an anchor line with itself plus the
    new lines, so the model copies the anchor, and a copy can alter it (a
    digit of a link changed in the comparison of past merges). It does not
    look at the proposal's wording: whether the right items were ticked or
    added is the model's call. Kept on its own so it is easy to remove if
    the evaluation shows it refuses good merges."""
    have = Counter(line.rstrip() for line in (merged or "").splitlines())
    unmatched = []
    for number, line in enumerate((previous or "").splitlines(), 1):
        if not _compared(line):
            continue
        line = line.rstrip()
        if have[line] > 0:
            have[line] -= 1
        else:
            unmatched.append((number, line))
    # Second pass, so an unchanged duplicate claims its exact line first.
    missing = []
    for number, line in unmatched:
        unticked = _UNTICKED_RE.match(line)
        ticked = next((t for t in (
            [f"{unticked.group(1)}[x]{unticked.group(2)}",
             f"{unticked.group(1)}[X]{unticked.group(2)}"]
            if unticked else []) if have[t] > 0), None)
        if ticked is None:
            missing.append((number, line))
        else:
            have[ticked] -= 1
    return missing


def kept_lines_error(missing):
    """The refusal for the model, naming the lines (they are its own
    input, so they may be quoted to it, never logged)."""
    named = "\n".join(f"line {number}: {line}"
                      for number, line in missing[:MAX_LINES_NAMED])
    more = len(missing) - MAX_LINES_NAMED
    return (
        f"{len(missing)} line(s) of the current list are changed or "
        "missing after your edits. Every existing line must stay exactly "
        "as it is; the only change allowed to one is `[ ]` becoming "
        "`[x]`. When a line is your old_text, copy it into new_text "
        "unchanged. The lines, as they are in the current list:\n"
        + named + (f"\n(and {more} more)" if more > 0 else ""))


def _unticked(line):
    """*line* as the nesting check matches it: trailing whitespace off
    and a ticked checkbox as `[ ]`, so a line the merge ticked is still
    the line it was."""
    line = line.rstrip()
    ticked = _TICKED_RE.match(line)
    return f"{ticked.group(1)}[ ]{ticked.group(2)}" if ticked else line


def _indent(line):
    """The width of the line's leading whitespace (a tab is 4)."""
    line = line.expandtabs(4)
    return len(line) - len(line.lstrip(" "))


def _parent_indexes(lines):
    """For each line, the index of the line it sits under: the nearest
    non-blank line above it at a smaller depth, not looking past a
    heading. None for a blank line, a heading, or a line with no such
    line above it.

    Depth is the indent // 2, as the Todo page (parseTodoSections) and the
    iOS app (TodoSections) read nesting: sub-items at 2 and 3 spaces are
    siblings, and a 1-space item is a top-level task. A tab counts as 4
    columns, as in CommonMark."""
    parents, open_lines = [], []   # (depth, index), depths increasing
    for index, line in enumerate(lines):
        if not line.strip():
            parents.append(None)
            continue
        if _HEADING_RE.match(line):
            open_lines = []
            parents.append(None)
            continue
        depth = _indent(line) // 2
        while open_lines and open_lines[-1][0] >= depth:
            open_lines.pop()
        parents.append(open_lines[-1][1] if open_lines else None)
        open_lines.append((depth, index))
    return parents


def lines_moved_under_new(previous, merged):
    """The nesting check: the lines of *previous* that sit under a line
    the edits added in *merged*, as [(line, the new line it sits under)].

    New lines between the sub-items of an existing task take the
    sub-items below them: a new top-level task inserted after a task's
    first sub-item gets the task's other sub-items as its own (GPT-6 Luna
    did this in the comparison of past merges, #234). Every line is
    still there, so lines_not_kept passes it.

    Refused only when it is certain, so a good merge is never refused:
    the line is an existing one (its text, ticked or not, is in the
    merged list as often as in the previous list, and sat under another
    line each time), and the line it now sits under is new (its text,
    ticked or not, is nowhere in the previous list). A new sub-item among
    existing ones is at their depth or deeper, so it is no line's
    parent. Kept on its own, like lines_not_kept, so it is easy to
    remove."""
    before = (previous or "").splitlines()
    after = (merged or "").splitlines()
    count_before = Counter(_unticked(line) for line in before
                           if line.strip())
    count_after = Counter(_unticked(line) for line in after if line.strip())
    had_parent = Counter(
        _unticked(before[index])
        for index, parent in enumerate(_parent_indexes(before))
        if parent is not None)
    moved = []
    for index, parent in enumerate(_parent_indexes(after)):
        if parent is None or not _compared(after[index]):
            continue
        line = _unticked(after[index])
        if (_unticked(after[parent]) not in count_before
                and count_before[line]
                and count_after[line] == count_before[line]
                and had_parent[line] == count_before[line]):
            moved.append((after[index].rstrip(), after[parent].rstrip()))
    return moved


def sub_items_moved_error(moved):
    """The refusal for the model, quoting each moved line and the new
    line it sits under (its own input and reply: never logged)."""
    named = "\n".join(f"`{line}` is now under `{parent}`"
                      for line, parent in moved[:MAX_LINES_NAMED])
    more = len(moved) - MAX_LINES_NAMED
    return (
        f"{len(moved)} existing line(s) now sit under a line your edits "
        "added, so they moved to another task. A new top-level task goes "
        "after the last sub-item of the task above it, never between the "
        "sub-items of an existing task. The lines:\n"
        + named + (f"\n(and {more} more)" if more > 0 else ""))


def resolve_merge_reply(content, current_todo, run):
    """The merged list for one reply: (merged, None, None), or
    (None, failure kind, reason for the model) with that kind counted on
    *run*."""
    edits, updated, error = parse_merge_reply(content)
    if error:
        run.format_errors += 1
        return None, FAILURE_FORMAT, error
    full_write = bool(updated.strip())
    if full_write:
        # Nothing to anchor edits on in an empty or template list (#410).
        if has_tasks(current_todo):
            run.rewrite_refusals += 1
            return None, FAILURE_REWRITE, REWRITE_REFUSED_ERROR
        merged = updated
    else:
        merged, error = apply_text_edits(current_todo or "", edits)
        if error:
            run.anchor_errors += 1
            return None, FAILURE_ANCHOR, error
    missing = lines_not_kept(current_todo, merged)
    if missing:
        run.kept_lines_failures += 1
        return None, FAILURE_KEPT_LINES, kept_lines_error(missing)
    moved = lines_moved_under_new(current_todo, merged)
    if moved:
        run.sub_items_moved_failures += 1
        return None, FAILURE_SUB_ITEMS_MOVED, sub_items_moved_error(moved)
    run.full_write = full_write
    run.edits_applied = 0 if full_write else len(edits)
    run.edits = None if full_write else edits
    return merged, None, None


# A list item's ticked checkbox, as rebase_merge clears it to compare.
_TICKED_BOX_RE = re.compile(
    r"^([ \t]*(?:[-*+]|\d+[.)])[ \t]+)\[[xX]\](?=[ \t]|$)", re.MULTILINE)


def _boxes_cleared(text):
    """*text* with every ticked list checkbox written `[ ]`. The length
    stays the same, so a position in it is the same position in *text*."""
    return _TICKED_BOX_RE.sub(lambda m: f"{m.group(1)}[ ]", text)


def _with_user_boxes(old_text, found, new_text):
    """*new_text* with the checkbox state the user gave, since the merge
    read the list, to the lines of *old_text* (*found* is that text as it
    is now; it differs from *old_text* in checkboxes only). The user's
    tick or untick wins over what the edit wrote for that line."""
    old_lines, found_lines = old_text.split("\n"), found.split("\n")
    new_lines = new_text.split("\n")
    changed = {i for i, (old, now) in enumerate(zip(old_lines, found_lines))
               if old != now}
    if not changed:
        return new_text
    # Which line of new_text is the edit's copy of each old_text line: the
    # same text, checkboxes aside, in the same order.
    matcher = SequenceMatcher(
        None, [_boxes_cleared(line) for line in old_lines],
        [_boxes_cleared(line) for line in new_lines], autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            if block.a + k in changed:
                new_lines[block.b + k] = found_lines[block.a + k]
    return "\n".join(new_lines)


def _reapply_edits(text, edits):
    """The merge's edits applied in order to *text*, the newest list, or
    None when one no longer fits. An edit whose old_text is in the list
    exactly once applies as it did. One that isn't, because the user
    ticked or unticked a line of it meanwhile, applies where its text is,
    checkboxes aside, exactly once; those lines keep the user's state."""
    for edit in edits:
        old, new = edit["old_text"], edit["new_text"]
        if text.count(old) == 1:
            text = text.replace(old, new, 1)
            continue
        cleared, old_cleared = _boxes_cleared(text), _boxes_cleared(old)
        if cleared.count(old_cleared) != 1:
            return None
        start = cleared.index(old_cleared)
        end = start + len(old)
        new = _with_user_boxes(old, text[start:end], new)
        text = text[:start] + new + text[end:]
    return text


def rebase_merge(run, previous, newest):
    """The merge's result built on *newest*, the list as it is when the
    merge saves, when it changed after the merge read *previous* (#477): a
    tick, the row "+", quick-add, an editor Save or a revert made while
    the model worked. The user's change is kept and the merge's edits are
    applied on top of it. Returns None when they no longer fit (an edit's
    line was changed or removed, or the merge wrote the whole list): the
    merge then saves nothing and the user can apply it again."""
    if newest == previous:
        return run.merged
    if run.edits is None:
        return None
    rebased = _reapply_edits(newest or "", run.edits)
    if rebased is None or not rebased.strip():
        return None
    # The same checks as for the model's reply, against the newest list.
    if (lines_not_kept(newest, rebased)
            or lines_moved_under_new(newest, rebased)):
        return None
    return rebased


def run_todo_merge(provider, model_id, messages, api_keys, current_todo,
                   run=None):
    """Call the model for the edits, apply them to *current_todo* and
    check the result, with one retry after a refused reply. Returns *run*
    (a MergeRun): ``merged`` on success, else ``failure``.

    *provider* is LLMProvider (or a stand-in with its get_completion). A
    provider error propagates; *run*, passed in, then still holds the
    responses of the calls made before it, whose cost the caller logs.
    """
    run = run if run is not None else MergeRun()
    messages = list(messages)
    for attempt in range(MAX_MERGE_ATTEMPTS):
        if attempt:
            run.retries += 1
        response = provider.get_completion(
            model_id, messages, api_keys, output_schema=TODO_EDITS_SCHEMA,
            output_schema_name=SCHEMA_NAME)
        run.responses.append(response)
        content = response.get("content") or ""
        run.replies.append(content)
        # Empty, then cut off: the order the task checked them in before.
        if not content.strip():
            run.failure = FAILURE_EMPTY
            return run
        # A cut-off reply is never applied (#432), not even a parseable
        # one: its last edits may be missing.
        if response.get("truncated"):
            run.failure = FAILURE_TRUNCATED
            return run
        merged, failure, error = resolve_merge_reply(
            content, current_todo, run)
        if failure is None:
            if not merged.strip():
                run.failure = FAILURE_EMPTY
                return run
            run.merged, run.failure, run.error = merged, None, None
            return run
        run.failure, run.error = failure, error
        run.refusals.append({"kind": failure, "reason": error})
        messages = messages + [
            {"role": "assistant",
             "content": [{"type": "text", "text": content}]},
            {"role": "user",
             "content": [{"type": "text",
                          "text": RETRY_MESSAGE.format(error=error)}]},
        ]
    return run
