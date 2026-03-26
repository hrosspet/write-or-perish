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
            result['category'] = body.strip().lower()
    return result
