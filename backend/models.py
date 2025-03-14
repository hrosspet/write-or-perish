from datetime import datetime
from flask_login import UserMixin
from backend.extensions import db

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    twitter_id = db.Column(db.String(64), unique=True, nullable=False)
    username = db.Column(db.String(64), unique=True, nullable=False)
    # A short description that the user may set (max 128 characters)
    description = db.Column(db.String(128), nullable=True, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationship to text nodes
    nodes = db.relationship("Node", backref="user", lazy=True)

    def get_id(self):
        return str(self.id)

class Node(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # The owner of this node (whether user–authored or created through LLM request)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # For tree structure: the parent node (if any)
    parent_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    # For linked nodes (see below): a pointer to the linked node (if applicable)
    linked_node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    # "user" for user–authored nodes, "llm" for LLM responses, and "link" for linked nodes.
    node_type = db.Column(db.String(16), nullable=False, default="user")
    # The actual (long-form) text
    content = db.Column(db.Text, nullable=False)
    # When an LLM response is created, save the total token count
    token_count = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Self–referential relationships.
    # “children” are nodes attached via parent_id.
    children = db.relationship("Node", backref=db.backref("parent", remote_side=[id]),
                               lazy=True, foreign_keys=[parent_id])
    # “linked_children” are nodes that link to this one via linked_node_id.
    linked_children = db.relationship("Node", backref=db.backref("linked_parent", remote_side=[id]),
                                      lazy=True, foreign_keys=[linked_node_id])
    
class NodeVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Which node this version belongs to
    node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=False)
    # The prior content of the node
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Optional backreference, if you need to access versions from a Node instance:
    node = db.relationship("Node", backref="versions")
