"""Shared helpers for voice tool call metadata."""
import json


def update_tool_meta(node, tool_name, updates):
    """Update a specific tool call's metadata in node.tool_calls_meta."""
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
    node.tool_calls_meta = json.dumps(meta)


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


def parse_share(content):
    """Parse ### Share and ### Share type from LLM text (SHARE_V1).

    Mirrors parse_feedback: the shareable text the AI proposes lives in the
    visible node content (so the user reads exactly what would be shared
    before confirming), never in a hidden tool input. Returns {'content',
    'share_type'} — share_type defaults blank if absent (the save path falls
    back to 'other')."""
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
        if heading == 'share':
            result['content'] = body
        elif heading == 'share type':
            first_line = body.split('\n')[0].strip().lower()
            result['share_type'] = first_line
    return result
