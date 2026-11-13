"""Admin: "Make ready from prepared material" (#698).

Under /api/admin/prefill-bundles. Every answer is metadata: the bundle's
manifest (X id, handle, counts, dates), the target account's state, and a
load's progress. No route reads or returns content.
"""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from backend.extensions import db
from backend.models import PrefillBundleLoad, User
from backend.routes.admin import admin_required
from backend.utils import prefill_bundles as pb

prefill_bundles_bp = Blueprint("prefill_bundles", __name__)

# How many recent loads the admin list shows.
RECENT_LOADS = 25


@prefill_bundles_bp.route("/config", methods=["GET"])
@login_required
@admin_required
def bundles_config():
    """Whether loading bundles is on here (the panel hides the section
    when it is off)."""
    return jsonify({"enabled": pb.bundles_enabled()}), 200


@prefill_bundles_bp.route("/check", methods=["GET"])
@login_required
@admin_required
def check_bundle():
    """?q=<handle or X id>: the bundle and the account it would go into."""
    from backend.utils.prefill_bundle_load import check
    from backend.utils.bundle_storage import StorageError
    try:
        result = check(request.args.get("q"))
    except StorageError as e:
        return jsonify({"error": str(e), "code": "storage"}), 502
    status = 200 if "error" not in result else (
        409 if result.get("code") in ("disabled", "excluded", "format",
                                      "manifest", "x_id_mismatch") else 404)
    return jsonify(result), status


@prefill_bundles_bp.route("/loads", methods=["POST"])
@login_required
@admin_required
def start_load():
    """Body: {"q": handle or X id, "force": bool, "merge": bool (default
    true: merge the loaded windows again with the account's merge model
    when another model wrote the bundle's final, one paid call)}.
    Returns 202 {"load"} or
    4xx {"error", "code"}; 502 {"code": "storage"} when the storage cannot
    be reached. The load lets nobody in: the admin approves as for any
    account."""
    from backend.utils.bundle_storage import StorageError
    from backend.utils.prefill_bundle_load import serialize_load, start_admin_load
    data = request.get_json() or {}
    try:
        load, refusal = start_admin_load(
            data.get("q"), current_user.id, force=bool(data.get("force")),
            merge=bool(data.get("merge", True)))
    except StorageError as e:
        db.session.rollback()
        return jsonify({"error": str(e), "code": "storage"}), 502
    if refusal:
        code, message = refusal
        return jsonify({"error": message, "code": code}), (
            404 if code == "no_bundle" else 409)
    return jsonify({"load": serialize_load(load)}), 202


@prefill_bundles_bp.route("/loads", methods=["GET"])
@login_required
@admin_required
def list_loads():
    from backend.utils.prefill_bundle_load import serialize_load
    loads = (PrefillBundleLoad.query
             .order_by(PrefillBundleLoad.id.desc()).limit(RECENT_LOADS).all())
    users = {u.id: u for u in User.query.filter(
        User.id.in_({ld.user_id for ld in loads})).all()} if loads else {}
    return jsonify({"loads": [serialize_load(ld, users.get(ld.user_id))
                              for ld in loads]}), 200


@prefill_bundles_bp.route("/loads/<int:load_id>", methods=["GET"])
@login_required
@admin_required
def get_load(load_id):
    from backend.utils.prefill_bundle_load import serialize_load
    load = db.session.get(PrefillBundleLoad, load_id)
    if load is None:
        return jsonify({"error": "No such load."}), 404
    return jsonify({"load": serialize_load(load)}), 200
