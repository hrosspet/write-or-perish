from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from datetime import datetime
import zipfile
import io
from werkzeug.utils import secure_filename

import_bp = Blueprint("import_bp", __name__)

def approximate_token_count(text):
    """
    Approximate token count for a text string.
    Uses a simple heuristic: ~4 characters per token.
    """
    return len(text) // 4


@import_bp.route("/import/analyze", methods=["POST"])
@login_required
def analyze_import():
    """
    Analyze a zip file containing .md documents for import.

    Expects:
        - multipart/form-data with 'zip_file' field

    Returns:
        {
            "files": [
                {
                    "name": "document.md",
                    "filename_without_ext": "document",
                    "content": "file content",
                    "size": 1234,
                    "created_at": "2024-01-01T12:00:00",
                    "modified_at": "2024-01-02T12:00:00",
                    "token_count": 300
                },
                ...
            ],
            "total_files": 5,
            "total_tokens": 1500,
            "total_size": 6789
        }
    """
    # Check if file is present
    if 'zip_file' not in request.files:
        return jsonify({"error": "No zip_file provided"}), 400

    zip_file = request.files['zip_file']

    if zip_file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    # Validate it's a zip file
    if not zip_file.filename.lower().endswith('.zip'):
        return jsonify({"error": "File must be a .zip file"}), 400

    try:
        # Read zip file into memory
        zip_bytes = io.BytesIO(zip_file.read())

        files_data = []
        total_tokens = 0
        total_size = 0

        with zipfile.ZipFile(zip_bytes, 'r') as zip_ref:
            # Get list of all .md files in the zip
            md_files = [f for f in zip_ref.namelist()
                       if f.lower().endswith('.md') and not f.startswith('__MACOSX/')]

            if not md_files:
                return jsonify({"error": "No .md files found in the zip archive"}), 400

            for file_path in md_files:
                # Get file info
                zip_info = zip_ref.getinfo(file_path)

                # Skip directories
                if zip_info.is_dir():
                    continue

                # Read file content
                try:
                    content = zip_ref.read(file_path).decode('utf-8')
                except UnicodeDecodeError:
                    # Skip files that can't be decoded as UTF-8
                    current_app.logger.warning(f"Skipping {file_path}: not valid UTF-8")
                    continue

                # Extract just the filename from the path
                filename = file_path.split('/')[-1]
                filename_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename

                # Get timestamps from zip metadata
                # ZipInfo date_time is a tuple: (year, month, day, hour, minute, second)
                dt_tuple = zip_info.date_time
                file_datetime = datetime(*dt_tuple)

                # Calculate tokens
                token_count = approximate_token_count(content)

                file_data = {
                    "name": filename,
                    "filename_without_ext": filename_without_ext,
                    "content": content,
                    "size": zip_info.file_size,
                    "modified_at": file_datetime.isoformat(),
                    "token_count": token_count
                }

                files_data.append(file_data)
                total_tokens += token_count
                total_size += zip_info.file_size

        if not files_data:
            return jsonify({"error": "No valid .md files could be read from the zip archive"}), 400

        return jsonify({
            "files": files_data,
            "total_files": len(files_data),
            "total_tokens": total_tokens,
            "total_size": total_size
        }), 200

    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid zip file"}), 400
    except Exception as e:
        current_app.logger.error(f"Error analyzing import: {str(e)}")
        return jsonify({"error": "Failed to analyze zip file", "details": str(e)}), 500


@import_bp.route("/import/confirm", methods=["POST"])
@login_required
def confirm_import():
    """
    Create nodes from analyzed import data.

    Request body:
        {
            "files": [
                {
                    "filename_without_ext": "document",
                    "content": "file content",
                    "modified_at": "2024-01-01T12:00:00"
                },
                ...
            ],
            "import_type": "single_thread" | "separate_nodes",
            "date_ordering": "modified" | "created"
        }

    Returns:
        {
            "message": "Import successful",
            "nodes_created": 5,
            "thread_count": 1 or 5
        }
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    files = data.get('files', [])
    import_type = data.get('import_type', 'separate_nodes')
    date_ordering = data.get('date_ordering', 'modified')

    if not files:
        return jsonify({"error": "No files provided"}), 400

    if import_type not in ['single_thread', 'separate_nodes']:
        return jsonify({"error": "Invalid import_type. Must be 'single_thread' or 'separate_nodes'"}), 400

    if date_ordering not in ['modified', 'created']:
        return jsonify({"error": "Invalid date_ordering. Must be 'modified' or 'created'"}), 400

    try:
        # Sort files by the selected date ordering
        # For now, we only have modified_at from zip metadata, so we'll use that
        files_sorted = sorted(files, key=lambda f: f.get('modified_at', ''))

        nodes_created = 0
        thread_count = 0

        if import_type == 'single_thread':
            # Create a single thread with all files as sequential nodes
            parent_node = None

            for file_data in files_sorted:
                filename = file_data.get('filename_without_ext', 'Untitled')
                content = file_data.get('content', '')

                # Add filename as markdown headline only if content doesn't have H1
                stripped_content = content.lstrip()
                if not stripped_content.startswith('# '):
                    node_content = f"# {filename}\n\n{content}"
                else:
                    node_content = content

                # Create node
                node = Node(
                    user_id=current_user.id,
                    parent_id=parent_node.id if parent_node else None,
                    node_type="user",
                    content=node_content,
                    token_count=approximate_token_count(node_content)
                )

                db.session.add(node)
                db.session.flush()  # Get the node ID

                parent_node = node
                nodes_created += 1

            thread_count = 1

        else:  # separate_nodes
            # Create separate top-level threads for each file
            for file_data in files_sorted:
                filename = file_data.get('filename_without_ext', 'Untitled')
                content = file_data.get('content', '')

                # Add filename as markdown headline only if content doesn't have H1
                stripped_content = content.lstrip()
                if not stripped_content.startswith('# '):
                    node_content = f"# {filename}\n\n{content}"
                else:
                    node_content = content

                # Create top-level node (parent_id=None)
                node = Node(
                    user_id=current_user.id,
                    parent_id=None,
                    node_type="user",
                    content=node_content,
                    token_count=approximate_token_count(node_content)
                )

                db.session.add(node)
                nodes_created += 1

            thread_count = len(files_sorted)

        # Commit all nodes
        db.session.commit()

        return jsonify({
            "message": "Import successful",
            "nodes_created": nodes_created,
            "thread_count": thread_count
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error confirming import: {str(e)}")
        return jsonify({"error": "Failed to import files", "details": str(e)}), 500
