import logging
from functools import wraps

from flask import Blueprint, redirect, render_template, request, session, url_for, jsonify

import firestore_helper

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint('onboarding', __name__, url_prefix='/onboarding')


def google_login_required(f):
    """Requires Google login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


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

    # Disciplines the athlete practices (multi-select)
    _VALID_DISC = {'run', 'bike', 'strength', 'yoga', 'swim', 'other'}
    disciplines = [d for d in (data.get('disciplines') or []) if d in _VALID_DISC]

    # Basic validation
    if not data.get('birth_date'):
        return jsonify({"error": "Campo requerido: birth_date"}), 400
    if not data.get('sex'):
        return jsonify({"error": "Campo requerido: sex"}), 400
    if not disciplines:
        return jsonify({"error": "Selecciona al menos una actividad"}), 400
    # Running volume is only required when the athlete runs
    if 'run' in disciplines:
        for field in ('weekly_days', 'weekly_km'):
            if not data.get(field) and data.get(field) != 0:
                return jsonify({"error": f"Campo requerido: {field}"}), 400

    def _i(key):
        try:
            return int(data.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _f(key):
        try:
            return float(data.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    # Sanitize: only keep expected fields
    assessment = {
        'birth_date':              str(data.get('birth_date', '')),
        'sex':                     str(data.get('sex', '')),
        'disciplines':             disciplines,
        'weekly_days':             _i('weekly_days'),
        'weekly_km':               _f('weekly_km'),
        'longest_run': {
            'distance_km':         _f('longest_run_km'),
            'time':                str(data.get('longest_run_time', '') or ''),
            'date':                str(data.get('longest_run_date', '') or ''),
        },
        'reference_times': {
            '5k':  str(data.get('ref_5k', '')  or ''),
            '10k': str(data.get('ref_10k', '') or ''),
            '21k': str(data.get('ref_21k', '') or ''),
            '42k': str(data.get('ref_42k', '') or ''),
        },
        'cycling': {
            'outdoor':             bool(data.get('bike_outdoor')),
            'indoor':              bool(data.get('bike_indoor')),
            'weekly_days':         _i('bike_weekly_days'),
            'weekly_km':           _f('bike_weekly_km'),
            'longest_ride_km':     _f('bike_longest_km'),
            'ftp':                 _i('bike_ftp'),
        },
        'cross_training': {
            'strength_days':       _i('strength_days'),
            'yoga_days':           _i('yoga_days'),
            'swim_days':           _i('swim_days'),
            'other_activity':      str(data.get('other_activity', '') or '')[:60],
            'other_days':          _i('other_days'),
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


