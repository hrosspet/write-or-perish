"""The signed-in user's invite link (#439). 404 unless PAYMENTS_ENABLED and
REFERRALS_ENABLED. Only the web's Account page calls it.

There is deliberately no route that turns a code into anything: a code
is read only at signup (routes/auth.py, utils/referrals.record_invite),
and nothing anywhere says whether a code belongs to an account.
"""
from flask import Blueprint, jsonify
from flask_login import current_user, login_required

from backend.utils.referrals import invite_url, referrals_enabled

referrals_bp = Blueprint("referrals_bp", __name__)


@referrals_bp.route("/link", methods=["GET"])
@login_required
def get_link():
    """{"url"}: the account's invite link, made on first use."""
    if not referrals_enabled():
        return jsonify({"error": "Not found"}), 404
    return jsonify({"url": invite_url(current_user)})
