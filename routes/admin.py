import json
import logging
import os
import uuid
from functools import wraps

from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for

import firestore_helper
from gcs_helper import GCSHelper
from helpers import process_dashboard_data

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

BUCKET_NAME = os.environ.get('GARMIN_BUCKET', 'garmin-dashboard-data')
DATA_FILE = 'training_data_monthly.json'


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        if not session.get('is_admin'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/')
@admin_required
def index():
    users = firestore_helper.get_all_users()
    return render_template('admin_dashboard.html', users=users)


@admin_bp.route('/user/<uid>')
@admin_required
def view_user(uid: str):
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)

    gcs = GCSHelper(BUCKET_NAME)
    data_blob = f"users/{uid}/{DATA_FILE}"
    raw_str = gcs.load_json(data_blob)
    if not raw_str:
        return f"<center><h3>No hay datos para {user.get('email', uid)}</h3></center>", 404

    raw_data = json.loads(raw_str)
    dashboard_data = process_dashboard_data(raw_data)
    dashboard_data['_admin_viewing'] = True
    dashboard_data['_admin_user_email'] = user.get('email', uid)
    dashboard_data['_viewed_uid'] = uid
    dashboard_data['refresh_count'] = 0
    dashboard_data['max_refresh'] = 0
    dashboard_data['last_refresh'] = user.get('last_refresh', '')

    return render_template('fitness_report.html', **dashboard_data)


@admin_bp.route('/user/<uid>/refresh', methods=['POST'])
@admin_required
def force_refresh(uid: str):
    """Incremental refresh: current month only."""
    from garmin_onboarding import is_refreshing
    if is_refreshing(uid):
        return jsonify({"error": "Ya hay una recarga en progreso para este usuario."}), 409

    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)

    from routes.dashboard import do_refresh
    user_name = user.get('display_name', 'Atleta').split()[0]
    training_goal = user.get('training_goal')
    response, status_code = do_refresh(uid, user_name=user_name, training_goal=training_goal)
    return response, status_code


@admin_bp.route('/user/<uid>/refresh/full', methods=['POST'])
@admin_required
def force_refresh_full(uid: str):
    """Full historical refresh: re-fetches last 6 months (async background thread)."""
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    if not user.get('garmin_connected'):
        return jsonify({"error": "Usuario sin Garmin conectado."}), 400

    from garmin_onboarding import fetch_initial_data_async
    gcs = GCSHelper(BUCKET_NAME)
    fetch_initial_data_async(uid, gcs, firestore_helper)
    logger.info(f"Admin triggered full refresh for {uid}")
    return jsonify({"status": "started"})


@admin_bp.route('/user/<uid>/request-reconnect', methods=['POST'])
@admin_required
def request_reconnect(uid: str):
    """Flag user to re-enter Garmin credentials on next login (e.g. after persistent 429)."""
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    gcs = GCSHelper(BUCKET_NAME)
    deleted = gcs.delete_directory(f"users/{uid}/tokens/")
    firestore_helper.upsert_user(uid, {'needs_garmin_reconnect': True})
    logger.info(f"Admin flagged {uid} for Garmin reconnect, deleted {deleted} token blobs.")
    return jsonify({"status": "flagged", "tokens_deleted": deleted})


@admin_bp.route('/user/<uid>/toggle-garmin-sync', methods=['POST'])
@admin_required
def toggle_garmin_sync(uid: str):
    """Enable or disable Garmin sync for a user. When disabled, no Garmin API calls are made."""
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    current = user.get('garmin_sync_disabled', False)
    new_val = not current
    firestore_helper.upsert_user(uid, {'garmin_sync_disabled': new_val})
    logger.info(f"Admin {'disabled' if new_val else 'enabled'} Garmin sync for {uid}")
    return jsonify({"status": "ok", "garmin_sync_disabled": new_val})


@admin_bp.route('/feedback')
@admin_required
def feedback_list():
    items = firestore_helper.get_all_feedback()
    unread = sum(1 for f in items if not f.get('read'))
    return render_template('admin_feedback.html', feedback=items, unread=unread)


@admin_bp.route('/feedback/<feedback_id>/read', methods=['POST'])
@admin_required
def mark_feedback_read(feedback_id: str):
    firestore_helper.mark_feedback_read(feedback_id)
    return jsonify({"status": "ok"})


@admin_bp.route('/coaching-rules', methods=['GET'])
@admin_required
def coaching_rules():
    rules = firestore_helper.get_coaching_rules()
    return render_template('coaching_rules.html', rules=rules)


@admin_bp.route('/coaching-rules', methods=['POST'])
@admin_required
def save_coaching_rule():
    """Add a new rule."""
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"error": "El texto de la regla no puede estar vacío."}), 400
    if len(text) > 500:
        return jsonify({"error": "Máximo 500 caracteres por regla."}), 400

    rules = firestore_helper.get_coaching_rules()
    rules.append({"id": str(uuid.uuid4()), "text": text, "active": True})
    firestore_helper.save_coaching_rules(rules)
    return jsonify({"status": "ok", "rules": rules})


@admin_bp.route('/coaching-rules/<rule_id>', methods=['PATCH'])
@admin_required
def update_coaching_rule(rule_id: str):
    """Toggle active or update text."""
    data = request.get_json(silent=True) or {}
    rules = firestore_helper.get_coaching_rules()
    for r in rules:
        if r.get('id') == rule_id:
            if 'active' in data:
                r['active'] = bool(data['active'])
            if 'text' in data:
                text = data['text'].strip()
                if text:
                    r['text'] = text[:500]
            break
    else:
        return jsonify({"error": "Regla no encontrada."}), 404
    firestore_helper.save_coaching_rules(rules)
    return jsonify({"status": "ok", "rules": rules})


@admin_bp.route('/coaching-rules/<rule_id>', methods=['DELETE'])
@admin_required
def delete_coaching_rule(rule_id: str):
    rules = firestore_helper.get_coaching_rules()
    rules = [r for r in rules if r.get('id') != rule_id]
    firestore_helper.save_coaching_rules(rules)
    return jsonify({"status": "ok", "rules": rules})


@admin_bp.route('/user/<uid>/recompute-summaries', methods=['POST'])
@admin_required
def recompute_summaries(uid: str):
    """Recomputes weekly summaries from stored GCS data — no Garmin API call needed."""
    import json as _json
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    gcs = GCSHelper(BUCKET_NAME)
    raw_str = gcs.load_json(f"users/{uid}/{DATA_FILE}")
    if not raw_str:
        return jsonify({"error": "No hay datos almacenados para este usuario."}), 404
    try:
        raw_data = _json.loads(raw_str)
        from weekly_summarizer import compute_weekly_summaries
        summaries = compute_weekly_summaries(raw_data)
        firestore_helper.save_weekly_summaries(uid, summaries)
        logger.info(f"Admin recomputed weekly summaries for {uid}: {len(summaries)} weeks")
        return jsonify({"status": "ok", "weeks": len(summaries)})
    except Exception as e:
        logger.error(f"Recompute summaries error for {uid}: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/weekly-summaries')
@admin_required
def weekly_summaries_view():
    """Shows weekly training summaries for all connected users."""
    users = firestore_helper.get_all_users()
    connected = [u for u in users if u.get('garmin_connected')]

    NUM_WEEKS = 10  # last N weeks to display

    user_data = []
    for u in sorted(connected, key=lambda x: (x.get('display_name') or x.get('email') or '')):
        uid = u.get('uid')
        weeks_dict = firestore_helper.get_weekly_summaries(uid)
        sorted_keys = sorted(weeks_dict.keys(), reverse=True)[:NUM_WEEKS]
        sorted_keys.reverse()
        weeks = [weeks_dict[k] for k in sorted_keys]
        user_data.append({
            'uid': uid,
            'name': u.get('display_name') or u.get('email', uid),
            'email': u.get('email', ''),
            'weeks': weeks,
        })

    return render_template('admin_weekly_summaries.html', user_data=user_data, num_weeks=NUM_WEEKS)


@admin_bp.route('/user/<uid>/refresh/full/status')
@admin_required
def full_refresh_status(uid: str):
    from garmin_onboarding import get_fetch_progress, is_refreshing
    progress = get_fetch_progress(uid)
    progress['running'] = is_refreshing(uid)
    return jsonify(progress)


@admin_bp.route('/refresh/status-all')
@admin_required
def refresh_status_all():
    """Returns refresh status for all users currently refreshing."""
    from garmin_onboarding import get_fetch_progress, is_refreshing
    users = firestore_helper.get_all_users()
    result = {}
    for u in users:
        uid = u.get('uid')
        if uid and is_refreshing(uid):
            result[uid] = get_fetch_progress(uid)
    return jsonify(result)
