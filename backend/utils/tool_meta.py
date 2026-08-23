"""Shared helpers for voice tool call metadata."""
import json
import re


def update_tool_meta(node, tool_name, updates, append_if_missing=False):
    """Update a specific tool call's metadata in node.tool_calls_meta.

    *append_if_missing* adds a fresh entry when none exists — used for
    user-authored share nodes, which never went through the LLM proposal
    flow and so have no propose_share entry to update."""
    meta = []
    if node.tool_calls_meta:
        try:
            meta = json.loads(node.tool_calls_meta)
        except (json.JSONDecodeError, TypeError):
            meta = []
    for entry in meta:
        if entry.get("name") == tool_name:
            entry.update(updates)
            break
    else:
        if append_if_missing:
            meta.append({"name": tool_name, **updates})
    node.tool_calls_meta = json.dumps(meta)


def get_tool_meta_entry(node, tool_name):
    """Return a specific tool call's metadata dict from node.tool_calls_meta,
    or None. Read-only companion to update_tool_meta."""
    if not node.tool_calls_meta:
        return None
    try:
        meta = json.loads(node.tool_calls_meta)
    except (json.JSONDecodeError, TypeError):
        return None
    for entry in meta:
        if entry.get("name") == tool_name:
            return entry
    return None


def parse_github_issue(content):
    """Parse ### Issue Title, ### Description, ### Category from LLM text."""
    result = {}
    parts = content.split('### ')
    for part in parts:
        if not part.strip():
            continue
        first_newline = part.find('\n')
        if first_newline < 0:
            continue
        heading = part[:first_newline].strip().lower()
        body = part[first_newline + 1:].strip()
        if 'issue title' in heading or heading == 'title':
            result['title'] = body
        elif heading == 'description':
            result['description'] = body
        elif heading == 'category':
            # Take only the first line to avoid trailing tags
            first_line = body.split('\n')[0].strip().lower()
            result['category'] = first_line
    return result


def parse_feedback(content):
    """Parse ### Feedback and ### Feedback category from LLM text.

    Mirrors parse_github_issue: the feedback the AI proposes to send lives in
    the visible node content (so the user reads it before confirming) rather
    than in a hidden tool input. Returns {'content', 'category'} — category
    defaults blank if absent (the submit path falls back to 'other')."""
    result = {}
    parts = content.split('### ')
    for part in parts:
        if not part.strip():
            continue
        first_newline = part.find('\n')
        if first_newline < 0:
            continue
        heading = part[:first_newline].strip().lower()
        body = part[first_newline + 1:].strip()
        if heading == 'feedback':
            result['content'] = body
        elif heading == 'feedback category':
            first_line = body.split('\n')[0].strip().lower()
            result['category'] = first_line
    return result


# A share block is fenced: an opening line `:::share <type>` (type word
# optional), the shareable text (any markdown, ### headings included),
# and a closing line of bare `:::`. An unclosed block runs to the end of
# the text. Fences replaced the legacy `### Share` / `### Share type`
# headings, which mis-parsed whenever the share body used ### itself and
# couldn't carry more than one post per node.
_SHARE_FENCE_RE = re.compile(
    r'^:::share(?:[ \t]+([A-Za-z]+))?[ \t]*\r?\n'
    r'(.*?)'
    r'(?:^:::[ \t]*$|\Z)',
    re.MULTILINE | re.DOTALL | re.IGNORECASE)


def parse_share(content):
    """Parse share blocks out of node text (SHARE_V1).

    Like parse_feedback, the shareable text lives in the visible node
    content (so the user reads exactly what would be shared before
    confirming), never in a hidden tool input. Returns {'shares':
    [{'content', 'share_type'}, ...]} — one entry per fenced `:::share`
    block, in order. share_type defaults blank if absent (the save path
    falls back to 'other').

    Falls back to the legacy `### Share` / `### Share type` headings
    (single share) when no fence is present, so pre-existing proposal
    nodes — and models imitating old conversation history — keep working.
    """
    shares = []
    for m in _SHARE_FENCE_RE.finditer(content or ""):
        body = (m.group(2) or "").strip()
        if not body:
            continue
        shares.append({
            'content': body,
            'share_type': (m.group(1) or "").strip().lower(),
        })
    if shares:
        return {'shares': shares}
    # Legacy heading fallback.
    legacy = {}
    parts = (content or "").split('### ')
    for part in parts:
        if not part.strip():
            continue
        first_newline = part.find('\n')
        if first_newline < 0:
            continue
        heading = part[:first_newline].strip().lower()
        body = part[first_newline + 1:].strip()
        if heading == 'share':
            legacy['content'] = body
        elif heading == 'share type':
            first_line = body.split('\n')[0].strip().lower()
            legacy['share_type'] = first_line
    if legacy.get('content'):
        return {'shares': [{
            'content': legacy['content'],
            'share_type': legacy.get('share_type', ''),
        }]}
    return {'shares': []}
