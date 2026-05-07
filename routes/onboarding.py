import logging
import os
from functools import wraps

from flask import Blueprint, redirect, render_template, request, session, url_for, jsonify
import re as _re

import firestore_helper
from gcs_helper import GCSHelper
from garmin_onboarding import login_garmin_and_save_tokens, fetch_initial_data_async, get_fetch_progress

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint('onboarding', __name__, url_prefix='/onboarding')

BUCKET_NAME = os.environ.get('GARMIN_BUCKET', 'garmin-dashboard-data')
DATA_FILE = 'training_data_monthly.json'


def google_login_required(f):
    """Requires Google login but does NOT require garmin_connected."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


@onboarding_bp.route('/')
@google_login_required
def warning():
    if session.get('garmin_connected'):
        return redirect(url_for('dashboard.index'))
    # If Garmin is globally disabled, auto-skip to CSV import flow
    config = firestore_helper.get_app_config()
    if not config.get('garmin_enabled', True):
        user_id = session['user_id']
        firestore_helper.upsert_user(user_id, {
            'garmin_connected': True,
            'garmin_sync_disabled': True,
        })
        session['garmin_connected'] = True
        session.pop('needs_login_refresh', None)
        return redirect(url_for('dashboard.index'))
    return render_template('onboarding.html', step='warning')


@onboarding_bp.route('/skip', methods=['POST'])
@google_login_required
def skip_garmin():
    """User chooses to continue without Garmin. Activates garmin_sync_disabled flow."""
    user_id = session['user_id']
    firestore_helper.upsert_user(user_id, {
        'garmin_connected': True,
        'garmin_sync_disabled': True,
    })
    session['garmin_connected'] = True
    session.pop('needs_login_refresh', None)
    return redirect(url_for('dashboard.index'))


@onboarding_bp.route('/connect', methods=['GET', 'POST'])
@google_login_required
def connect():
    if session.get('garmin_connected'):
        return redirect(url_for('dashboard.index'))

    if request.method == 'GET':
        return render_template('onboarding.html', step='connect', error=None)

    garmin_email = request.form.get('garmin_email', '').strip()
    garmin_password = request.form.get('garmin_password', '')

    if not garmin_email or not garmin_password:
        return render_template('onboarding.html', step='connect',
                               error="Debes ingresar tu email y contraseña de Garmin.")

    user_id = session['user_id']
    gcs = GCSHelper(BUCKET_NAME)

    success, error_msg = login_garmin_and_save_tokens(garmin_email, garmin_password, user_id, gcs)

    if not success:
        return render_template('onboarding.html', step='connect', error=error_msg)

    firestore_helper.upsert_user(user_id, {
        'garmin_connected': True,
        'garmin_token_path': f"users/{user_id}/tokens/",
        'garmin_data_path': f"users/{user_id}/{DATA_FILE}",
        'needs_garmin_reconnect': False,
    })
    session['garmin_connected'] = True

    import firestore_helper as fh
    fetch_initial_data_async(user_id, gcs, fh)

    return redirect(url_for('onboarding.pending'))


@onboarding_bp.route('/pending')
@google_login_required
def pending():
    return render_template('onboarding.html', step='pending')


@onboarding_bp.route('/profile', methods=['GET'])
@google_login_required
def profile_setup():
    user_name = session.get('display_name', 'Atleta').split()[0]
    return render_template('profile_setup.html', user_name=user_name)


@onboarding_bp.route('/profile', methods=['POST'])
@google_login_required
def profile_setup_save():
    data = request.get_json(silent=True) or {}
    uid = session['user_id']

    # Basic validation
    required = ['birth_date', 'sex', 'weekly_days', 'weekly_km']
    for field in required:
        if not data.get(field) and data.get(field) != 0:
            return jsonify({"error": f"Campo requerido: {field}"}), 400

    # Sanitize: only keep expected fields
    assessment = {
        'birth_date':              str(data.get('birth_date', '')),
        'sex':                     str(data.get('sex', '')),
        'weekly_days':             int(data.get('weekly_days', 0)),
        'weekly_km':               float(data.get('weekly_km', 0)),
        'longest_run': {
            'distance_km':         float(data.get('longest_run_km', 0) or 0),
            'time':                str(data.get('longest_run_time', '') or ''),
            'date':                str(data.get('longest_run_date', '') or ''),
        },
        'reference_times': {
            '5k':  str(data.get('ref_5k', '')  or ''),
            '10k': str(data.get('ref_10k', '') or ''),
            '21k': str(data.get('ref_21k', '') or ''),
            '42k': str(data.get('ref_42k', '') or ''),
        },
        'long_run_day':            str(data.get('long_run_day', '') or ''),
        'plan_start_preference':   str(data.get('plan_start_preference', 'now') or 'now'),
        'plan_start_date':         str(data.get('plan_start_date', '') or ''),
    }

    firestore_helper.save_assessment(uid, assessment)
    session['has_profile_assessment'] = True
    return jsonify({"status": "ok"})


@onboarding_bp.route('/profile/skip', methods=['POST'])
@google_login_required
def profile_setup_skip():
    """User skips the assessment. We mark session so they're not redirected again this session."""
    session['has_profile_assessment'] = True
    return jsonify({"status": "ok"})


@onboarding_bp.route('/status')
@google_login_required
def status():
    user_id = session['user_id']
    progress = get_fetch_progress(user_id)
    pct = progress.get("pct", 0)

    # Only check GCS when progress reports 100% or is unknown (pct=0, no thread started)
    ready = False
    if pct == 100 or pct == 0:
        gcs = GCSHelper(BUCKET_NAME)
        data_blob = f"users/{user_id}/{DATA_FILE}"
        ready = gcs.load_json(data_blob) is not None

    return jsonify({
        "ready": ready,
        "pct": pct,
        "msg": progress.get("msg", "Iniciando..."),
        "error": pct == -1,
    })
