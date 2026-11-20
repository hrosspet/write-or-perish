"""The signed-in user's invite (#439; Peter, 2026-11-13: one personal
invite a month). 404 unless PAYMENTS_ENABLED and REFERRALS_ENABLED. Only
the web's Account page calls these.

There is deliberately no route that turns a code into anything: a code
is read only at a sign-up (routes/auth.py, utils/referrals.redeem_invite),
and nothing anywhere says whether a code exists, or for whom.
"""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from backend.utils.referrals import (
    InviteRefused, create_invite, invite_state, referrals_enabled,
    withdraw_invite)

referrals_bp = Blueprint("referrals_bp", __name__)


def _not_found():
    return jsonify({"error": "Not found"}), 404


@referrals_bp.route("/invite", methods=["GET"])
@login_required
def get_invite():
    """{"invite": {"url", "kind", "to", "expires_at"} | null, "can_invite",
    "next_invite_on", "credit"}: the open invite, or whether one may be
    made now, or the day the next one comes."""
    if not referrals_enabled():
        return _not_found()
    return jsonify(invite_state(current_user))


@referrals_bp.route("/invite", methods=["POST"])
@login_required
def make_invite():
    """{"to": an X handle or an email address}: this month's invite, for
    that person."""
    if not referrals_enabled():
        return _not_found()
    data = request.get_json(silent=True) or {}
    try:
        create_invite(current_user, data.get("to"))
    except InviteRefused as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(invite_state(current_user)), 201


@referrals_bp.route("/invite", methods=["DELETE"])
@login_required
def withdraw():
    """Withdraw the open invite (its handle or address is deleted)."""
    if not referrals_enabled():
        return _not_found()
    withdraw_invite(current_user)
    return jsonify(invite_state(current_user))
