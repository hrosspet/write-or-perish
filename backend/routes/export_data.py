from flask import Blueprint, jsonify, Response
from flask_login import login_required, current_user
from backend.models import Node, NodeVersion
from backend.extensions import db
from datetime import datetime

export_bp = Blueprint("export_bp", __name__)

# Export all of the current user’s data (in JSON format for MVP).
@export_bp.route("/export", methods=["GET"])
@login_required
def export_data():
    user_data = {
        "user": {
            "id": current_user.id,
            "twitter_id": current_user.twitter_id,
            "username": current_user.username,
            "description": current_user.description,
            "created_at": current_user.created_at.isoformat(),
        },
        "nodes": [],
        "versions": []
    }
    nodes = Node.query.filter_by(user_id=current_user.id).all()
    for node in nodes:
        user_data["nodes"].append({
            "id": node.id,
            "content": node.content,
            "node_type": node.node_type,
            "parent_id": node.parent_id,
            "linked_node_id": node.linked_node_id,
            "token_count": node.token_count,
            "created_at": node.created_at.isoformat(),
            "updated_at": node.updated_at.isoformat()
        })
    versions = NodeVersion.query.join(Node, Node.id == NodeVersion.node_id).filter(Node.user_id == current_user.id).all()
    for version in versions:
        user_data["versions"].append({
            "id": version.id,
            "node_id": version.node_id,
            "content": version.content,
            "timestamp": version.timestamp.isoformat()
        })
    return jsonify(user_data), 200

def format_node_tree(node, prefix="", index_path="1", is_last=True, processed_nodes=None):
    """
    Recursively format a node and its descendants into a human-readable tree structure.

    Args:
        node: The Node object to format
        prefix: The indentation prefix for the current node
        index_path: The hierarchical index (e.g., "1.1.2")
        is_last: Whether this is the last child of its parent
        processed_nodes: Set of node IDs already processed (to avoid infinite loops)

    Returns:
        str: Formatted text representation of the node tree
    """
    if processed_nodes is None:
        processed_nodes = set()

    # Avoid infinite loops from circular references
    if node.id in processed_nodes:
        return ""
    processed_nodes.add(node.id)

    # Format the node header
    author = node.user.username if node.user else "Unknown"
    node_type_display = "AI" if node.node_type == "llm" else "User"
    if node.node_type == "llm" and node.llm_model:
        node_type_display = f"AI ({node.llm_model})"

    timestamp = node.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Build the node text
    indent = "  " * (len(index_path.split('.')) - 1)
    separator = "─" * 79

    result = f"{indent}[{index_path}] {node_type_display} ({author}) - {timestamp}\n"
    result += f"{indent}{separator}\n"

    # Add the content with proper indentation
    content_lines = node.content.split('\n')
    for line in content_lines:
        result += f"{indent}{line}\n"
    result += "\n"

    # Process children
    children = sorted(node.children, key=lambda c: c.created_at)
    for i, child in enumerate(children):
        is_last_child = (i == len(children) - 1)
        child_index = f"{index_path}.{i+1}"

        # Mark branches (when there are multiple children)
        if len(children) > 1 and i > 0:
            result += f"{indent}  *** BRANCH ***\n\n"

        result += format_node_tree(
            child,
            prefix=prefix,
            index_path=child_index,
            is_last=is_last_child,
            processed_nodes=processed_nodes
        )

    return result

@export_bp.route("/export/threads", methods=["GET"])
@login_required
def export_threads():
    """
    Export all threads originated by the current user in a human-readable text format.

    This includes:
    - All top-level nodes created by the user
    - All descendants of those nodes (including AI replies)
    - Properly formatted with hierarchical structure showing branches
    """
    # Get all top-level nodes (threads) created by the user
    top_level_nodes = Node.query.filter_by(
        user_id=current_user.id,
        parent_id=None
    ).order_by(Node.created_at).all()

    if not top_level_nodes:
        return Response(
            "No threads found to export.",
            mimetype="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="write-or-perish-export-{datetime.utcnow().strftime("%Y%m%d-%H%M%S")}.txt"'
            }
        )

    # Build the export content
    export_lines = []
    export_lines.append("=" * 80)
    export_lines.append("Write or Perish - Thread Export")
    export_lines.append(f"User: {current_user.username}")
    export_lines.append(f"Export Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    export_lines.append(f"Total Threads: {len(top_level_nodes)}")
    export_lines.append("=" * 80)
    export_lines.append("")

    # Process each thread
    for thread_num, node in enumerate(top_level_nodes, 1):
        # Thread header
        preview = node.content[:60].replace('\n', ' ')
        if len(node.content) > 60:
            preview += "..."

        export_lines.append("")
        export_lines.append("=" * 80)
        export_lines.append(f"THREAD {thread_num}: \"{preview}\"")
        export_lines.append(f"Started: {node.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        export_lines.append("=" * 80)
        export_lines.append("")

        # Format the entire thread tree
        thread_text = format_node_tree(node, index_path=str(thread_num))
        export_lines.append(thread_text)

        export_lines.append("")

    # Add footer
    export_lines.append("=" * 80)
    export_lines.append("End of Export")
    export_lines.append("=" * 80)

    export_content = "\n".join(export_lines)

    # Return as downloadable text file
    filename = f"write-or-perish-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"
    return Response(
        export_content,
        mimetype="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

# Delete all of the current user's data from our app.
@export_bp.route("/delete_my_data", methods=["DELETE"])
@login_required
def delete_my_data():
    try:
        # Delete all node versions first, then nodes.
        NodeVersion.query.filter(
            NodeVersion.node_id.in_(db.session.query(Node.id).filter_by(user_id=current_user.id))
        ).delete(synchronize_session=False)
        Node.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Error deleting data", "details": str(e)}), 500
    return jsonify({"message": "All your app data has been deleted."}), 200
