import logging
import os
from functools import wraps

from flask import Blueprint, redirect, render_template, request, session, url_for, jsonify

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
