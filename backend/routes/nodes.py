from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from backend.models import Node, NodeVersion
from backend.extensions import db
from datetime import datetime
from openai import OpenAI

nodes_bp = Blueprint("nodes_bp", __name__)



# Create a new node (a “text bubble”)
@nodes_bp.route("/", methods=["POST"])
@login_required
def create_node():
    data = request.get_json()
    content = data.get("content")
    if not content:
        return jsonify({"error": "Content is required"}), 400
    parent_id = data.get("parent_id")  # May be None for a root node.
    node_type = data.get("node_type", "user")  # default is "user"
    linked_node_id = data.get("linked_node_id")  # For linked nodes
    node = Node(
        user_id=current_user.id,
        parent_id=parent_id,
        node_type=node_type,
        content=content,
        linked_node_id=linked_node_id,
    )
    db.session.add(node)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "DB error creating node"}), 500
    return jsonify({
        "id": node.id,
        "content": node.content,
        "node_type": node.node_type,
        "parent_id": node.parent_id,
        "linked_node_id": node.linked_node_id,
        "created_at": node.created_at.isoformat()
    }), 201

# Update (edit) a node. (The node’s prior content is saved in NodeVersion.)
@nodes_bp.route("/<int:node_id>", methods=["PUT"])
@login_required
def update_node(node_id):
    node = Node.query.get_or_404(node_id)
    if node.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    data = request.get_json()
    new_content = data.get("content")
    if new_content is None:
        return jsonify({"error": "Content required for update"}), 400
    # Save the current version before update.
    version = NodeVersion(node_id=node.id, content=node.content)
    db.session.add(version)
    node.content = new_content
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "DB error updating node"}), 500
    return jsonify({"message": "Node updated", "node": {
        "id": node.id,
        "content": node.content,
        "node_type": node.node_type,
        "updated_at": node.updated_at.isoformat()
    }}), 200

# Retrieve a node with its full content (the highlighted node) plus previews of its children.
@nodes_bp.route("/<int:node_id>", methods=["GET"])
@login_required
def get_node(node_id):
    node = Node.query.get_or_404(node_id)
    # For non-highlighted nodes elsewhere, you might return just a preview (e.g. first 200 characters).
    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")
    # Get immediate children (previews only)
    children = Node.query.filter_by(parent_id=node_id).all()
    children_list = [{
        "id": child.id,
        "preview": make_preview(child.content),
        "child_count": len(child.children),
        "node_type": child.node_type,
    } for child in children]
    response = {
        "id": node.id,
        "content": node.content,  # full text because it’s highlighted.
        "node_type": node.node_type,
        "child_count": len(node.children),
        "children": children_list,
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat()
    }
    return jsonify(response), 200

# Retrieve children of a node (as previews).
@nodes_bp.route("/<int:node_id>/children", methods=["GET"])
@login_required
def get_children(node_id):
    node = Node.query.get_or_404(node_id)
    def make_preview(text, length=200):
        return text[:length] + ("..." if len(text) > length else "")
    children = Node.query.filter_by(parent_id=node_id).all()
    children_list = [{
        "id": child.id,
        "preview": make_preview(child.content),
        "child_count": len(child.children),
        "node_type": child.node_type,
    } for child in children]
    return jsonify({"children": children_list}), 200

# Request an LLM response based on the thread (the ancestors’ texts are joined as a prompt).
@nodes_bp.route("/<int:node_id>/llm", methods=["POST"])
@login_required
def request_llm_response(node_id):
    parent_node = Node.query.get_or_404(node_id)
    
    # Build the prompt by traversing up the parent chain.
    prompt_texts = []
    current = parent_node
    while current:
        prompt_texts.append(current.content)
        current = current.parent
    prompt = "\n".join(reversed(prompt_texts))
    
    # Initialize the OpenAI client using the API key from config.
    api_key = current_app.config.get("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key)
    
    try:
        response = client.chat.completions.create(
            model="gpt-4.5-preview",
            messages=[
                {"role": "system", "content": "You are an assistant."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "text"},
            temperature=1,
            max_completion_tokens=10000,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
        )
    except Exception as e:
        current_app.logger.error("OpenAI API error: %s", e)
        return jsonify({"error": "OpenAI API error", "details": str(e)}), 500

    # Using dot notation to extract the completion text.
    try:
        llm_text = response.choices[0].message.content
    except Exception as e:
        current_app.logger.error("Error extracting LLM text: %s", e)
        return jsonify({"error": "Error parsing LLM response"}), 500

    # Extract usage details if available.
    total_tokens = response.usage.total_tokens if response.usage else None

    # Save the LLM response as a new child node.
    llm_node = Node(
        user_id=current_user.id,
        parent_id=parent_node.id,
        node_type="llm",
        content=llm_text,
        token_count=total_tokens
    )
    db.session.add(llm_node)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error("DB error saving LLM response: %s", e)
        return jsonify({"error": "DB error saving LLM response"}), 500

    return jsonify({
        "message": "LLM response created",
        "node": {
            "id": llm_node.id,
            "content": llm_node.content,
            "token_count": llm_node.token_count,
            "created_at": llm_node.created_at.isoformat()
        }
    }), 201

# Create a linked node – allowing the user to reference another node either as a link alone or with additional text.
@nodes_bp.route("/<int:node_id>/link", methods=["POST"])
@login_required
def add_linked_node(node_id):
    parent_node = Node.query.get_or_404(node_id)
    data = request.get_json()
    linked_node_id = data.get("linked_node_id")
    additional_text = data.get("content", "")  # Optional extra text.
    if not linked_node_id:
        return jsonify({"error": "linked_node_id is required"}), 400
    # Validate that the node to be linked exists.
    linked_node = Node.query.get(linked_node_id)
    if not linked_node:
        return jsonify({"error": "Linked node not found"}), 404
    new_node = Node(
        user_id=current_user.id,
        parent_id=parent_node.id,
        node_type="link",
        content=additional_text,
        linked_node_id=linked_node_id
    )
    db.session.add(new_node)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "DB error adding linked node"}), 500
    return jsonify({
        "message": "Linked node added",
        "node": {
            "id": new_node.id,
            "content": new_node.content,
            "node_type": new_node.node_type,
            "linked_node_id": new_node.linked_node_id,
            "created_at": new_node.created_at.isoformat()
        }
    }), 201
