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
    accepted_terms_at = db.Column(db.DateTime, nullable=True)
    
    # Relationship to text nodes
    nodes = db.relationship("Node", backref="user", lazy=True)

    def get_id(self):
        return str(self.id)

class Node(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    linked_node_id = db.Column(db.Integer, db.ForeignKey("node.id"), nullable=True)
    node_type = db.Column(db.String(16), nullable=False, default="user")
    content = db.Column(db.Text, nullable=False)
    token_count = db.Column(db.Integer, nullable=True)
    # NEW: distributed_tokens will hold the portion of an LLM response allocated to this node’s author.
    distributed_tokens = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Self–referential relationships…
    children = db.relationship("Node", backref=db.backref("parent", remote_side=[id]),
                               lazy=True, foreign_keys=[parent_id])
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
