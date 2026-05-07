import logging
from functools import wraps
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import firestore_helper

logger = logging.getLogger(__name__)

user_bp = Blueprint('user', __name__, url_prefix='/user')

_SUPPORTED_LOCALES = {'es', 'en'}


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


@user_bp.route('/lang', methods=['POST'])
def set_lang():
    data = request.get_json(silent=True) or {}
    lang = (data.get('lang') or '').strip().lower()
    if lang not in _SUPPORTED_LOCALES:
        return jsonify({"error": f"Idioma no soportado. Usa: {', '.join(sorted(_SUPPORTED_LOCALES))}"}), 400
    session['lang'] = lang
    if 'user_id' in session:
        firestore_helper.upsert_user(session['user_id'], {'lang': lang})
        logger.info(f"User {session['user_id']} set lang to {lang}")
    return jsonify({"status": "ok", "lang": lang})


@user_bp.route('/profile', methods=['GET'])
@_login_required
def profile_view():
    uid = session['user_id']
    assessment = firestore_helper.get_latest_assessment(uid) or {}
    longest = assessment.get('longest_run', {})
    ref = assessment.get('reference_times', {})
    from tz_utils import today_tz
    today_str = today_tz(session.get('timezone')).isoformat()
    return render_template(
        'user_profile.html',
        display_name=session.get('display_name', ''),
        email=session.get('email', ''),
        picture_url=session.get('picture_url', ''),
        lang=session.get('lang', 'es'),
        today=today_str,
        birth_date=assessment.get('birth_date', ''),
        sex=assessment.get('sex', ''),
        weekly_days=assessment.get('weekly_days', ''),
        weekly_km=assessment.get('weekly_km', ''),
        longest_run_km=longest.get('distance_km', ''),
        longest_run_time=longest.get('time', ''),
        longest_run_date=longest.get('date', ''),
        ref_5k=ref.get('5k', ''),
        ref_10k=ref.get('10k', ''),
        ref_21k=ref.get('21k', ''),
        ref_42k=ref.get('42k', ''),
        long_run_day=assessment.get('long_run_day', ''),
    )


@user_bp.route('/profile', methods=['POST'])
@_login_required
def profile_save():
    data = request.get_json(silent=True) or {}
    uid = session['user_id']

    assessment = {
        'birth_date':    str(data.get('birth_date', '') or ''),
        'sex':           str(data.get('sex', '') or ''),
        'weekly_days':   int(data.get('weekly_days', 0) or 0),
        'weekly_km':     float(data.get('weekly_km', 0) or 0),
        'longest_run': {
            'distance_km': float(data.get('longest_run_km', 0) or 0),
            'time':        str(data.get('longest_run_time', '') or ''),
            'date':        str(data.get('longest_run_date', '') or ''),
        },
        'reference_times': {
            '5k':  str(data.get('ref_5k', '')  or ''),
            '10k': str(data.get('ref_10k', '') or ''),
            '21k': str(data.get('ref_21k', '') or ''),
            '42k': str(data.get('ref_42k', '') or ''),
        },
        'long_run_day': str(data.get('long_run_day', '') or ''),
    }

    firestore_helper.save_assessment(uid, assessment)
    session['has_profile_assessment'] = True
    logger.info(f"User {uid} updated profile via /user/profile")
    return jsonify({"status": "ok"})
