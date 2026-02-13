"""
One-time script to delete imported Claude nodes.

Deletes all nodes for a given user where updated_at is after the cutoff.
Imported nodes have updated_at set to import time, while created_at
reflects the original Claude timestamp — so updated_at is the right filter.

Usage (on the server):
    cd /path/to/write-or-perish
    python scripts/delete_imported_nodes.py
"""
from datetime import datetime
from backend import create_app
from backend.extensions import db
from backend.models import Node, User

# The user's last hand-created node was displayed as 13/02/2026, 11:55:10
# in local time (CET = UTC+1), so UTC would be 10:55:10.
# Using 10:56:00 UTC to safely exclude that node.
CUTOFF_UTC = datetime(2026, 2, 13, 10, 56, 0)
USERNAME = "hrosspet"

app = create_app()

with app.app_context():
    user = User.query.filter_by(username=USERNAME).first()
    if not user:
        print(f"User '{USERNAME}' not found!")
        exit(1)

    nodes = Node.query.filter(
        Node.user_id == user.id,
        Node.updated_at > CUTOFF_UTC
    ).order_by(Node.updated_at).all()

    # Also find claude-web nodes (assistant messages from import)
    claude_user = User.query.filter_by(username="claude-web").first()
    claude_nodes = []
    if claude_user:
        claude_nodes = Node.query.filter(
            Node.user_id == claude_user.id,
            Node.updated_at > CUTOFF_UTC
        ).order_by(Node.updated_at).all()

    all_nodes = nodes + claude_nodes

    if not all_nodes:
        print("No nodes found matching the criteria.")
        exit(0)

    print(f"Found {len(all_nodes)} nodes to delete:")
    print(f"  - {len(nodes)} owned by '{USERNAME}'")
    print(f"  - {len(claude_nodes)} owned by 'claude-web'")
    print()

    for n in all_nodes[:10]:
        preview = (n.get_content() or "")[:80].replace("\n", " ")
        print(f"  id={n.id}  type={n.node_type}  "
              f"created={n.created_at}  updated={n.updated_at}")
        print(f"    {preview}")
    if len(all_nodes) > 10:
        print(f"  ... and {len(all_nodes) - 10} more")

    print()
    confirm = input("Delete these nodes? (yes/no): ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        exit(0)

    for n in all_nodes:
        db.session.delete(n)

    db.session.commit()
    print(f"Deleted {len(all_nodes)} nodes.")
