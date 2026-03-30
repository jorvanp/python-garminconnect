import csv
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import threading
from functools import wraps

from tz_utils import now_cdmx, today_cdmx, today_tz

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for, abort

import firestore_helper
from gcs_helper import GCSHelper
from helpers import process_dashboard_data

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint('dashboard', __name__)

BUCKET_NAME = os.environ.get('GARMIN_BUCKET', 'garmin-dashboard-data')
DATA_FILE = 'training_data_monthly.json'


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        if not session.get('garmin_connected'):
            return redirect(url_for('onboarding.warning'))
        return f(*args, **kwargs)
    return decorated


def _gcs():
    return GCSHelper(BUCKET_NAME)


def _load_data(user_id: str) -> dict | None:
    raw = _gcs().load_json(f"users/{user_id}/{DATA_FILE}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


def _save_data(user_id: str, data: dict):
    _gcs().save_json(f"users/{user_id}/{DATA_FILE}", json.dumps(data, indent=2, ensure_ascii=False))


def _get_goal(user_id: str) -> dict | None:
    user_doc = firestore_helper.get_user(user_id) or {}
    return user_doc.get('training_goal')


def _get_training_plan(user_id: str) -> dict | None:
    user_doc = firestore_helper.get_user(user_id) or {}
    return user_doc.get('training_plan')


_ACTIVITY_TYPE_MAP = {
    'Correr': {'typeId': 1, 'typeKey': 'running'},
    'Entrenamiento en cinta': {'typeId': 1, 'typeKey': 'treadmill_running'},
    'Carrera en cinta': {'typeId': 1, 'typeKey': 'treadmill_running'},
    'Ciclismo': {'typeId': 2, 'typeKey': 'cycling'},
    'Ciclismo indoor': {'typeId': 25, 'typeKey': 'indoor_cycling'},
    'Natación': {'typeId': 4, 'typeKey': 'lap_swimming'},
    'Natación en aguas abiertas': {'typeId': 4, 'typeKey': 'open_water_swimming'},
    'Caminar': {'typeId': 9, 'typeKey': 'walking'},
    'Senderismo': {'typeId': 3, 'typeKey': 'hiking'},
    'Entreno de fuerza': {'typeId': 13, 'typeKey': 'strength_training'},
    'Cardio': {'typeId': 26, 'typeKey': 'cardio_training'},
    'Yoga': {'typeId': 41, 'typeKey': 'yoga'},
    'Elíptica': {'typeId': 38, 'typeKey': 'elliptical'},
    'Escaladora': {'typeId': 37, 'typeKey': 'stair_climbing'},
    'Esquí': {'typeId': 15, 'typeKey': 'resort_skiing_snowboarding'},
    'Tenis': {'typeId': 149, 'typeKey': 'tennis'},
    'Fútbol': {'typeId': 21, 'typeKey': 'soccer'},
    'Otro': {'typeId': 153, 'typeKey': 'other'},
}


def _parse_csv_row(row: dict) -> dict | None:
    """Convert a Garmin CSV export row (Spanish headers) to the internal activity format."""
    def _num(val, default=None):
        if not val or str(val).strip() in ('--', ''):
            return default
        try:
            return float(str(val).replace(',', '').replace(' ', ''))
        except (ValueError, TypeError):
            return default

    def _duration(val):
        """HH:MM:SS or MM:SS → seconds."""
        if not val or str(val).strip() in ('--', ''):
            return None
        parts = str(val).strip().split(':')
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except (ValueError, TypeError):
            pass
        return None

    def _pace_to_speed(val):
        """MM:SS /km → m/s."""
        if not val or str(val).strip() in ('--', ''):
            return None
        parts = str(val).strip().split(':')
        try:
            if len(parts) == 2:
                secs_per_km = int(parts[0]) * 60 + int(parts[1])
                return round(1000 / secs_per_km, 4) if secs_per_km > 0 else None
        except (ValueError, TypeError):
            pass
        return None

    start_time = row.get('Fecha', '').strip()
    if not start_time:
        return None

    act_type_str = row.get('Tipo de actividad', 'Otro').strip()
    act_type = _ACTIVITY_TYPE_MAP.get(act_type_str, {'typeId': 153, 'typeKey': 'other'})

    distance_km = _num(row.get('Distancia', ''))
    duration_secs = _duration(row.get('Tiempo', '')) or 0.0

    # Stable fake activityId derived from start time (won't conflict with real Garmin IDs)
    fake_id = int(hashlib.md5(start_time.encode()).hexdigest()[:10], 16) % (10 ** 10)

    return {
        'activityId': fake_id,
        'activityName': row.get('Título', act_type_str).strip().strip('"'),
        'startTimeLocal': start_time,
        'startTimeGMT': start_time,
        'activityType': {**act_type, 'parentTypeId': 17, 'isHidden': False, 'restricted': False, 'trimmable': True},
        'eventType': {'typeId': 9, 'typeKey': 'uncategorized', 'sortOrder': 10},
        'distance': (distance_km * 1000) if distance_km is not None else 0.0,
        'duration': duration_secs,
        'elapsedDuration': _duration(row.get('Tiempo transcurrido', '')) or duration_secs,
        'movingDuration': _duration(row.get('Tiempo en movimiento', '')) or duration_secs,
        'elevationGain': _num(row.get('Ascenso total', '')) or 0.0,
        'elevationLoss': _num(row.get('Descenso total', '')) or 0.0,
        'averageSpeed': _pace_to_speed(row.get('Ritmo medio', '')),
        'calories': _num(row.get('Calorías', '')) or 0.0,
        'averageHR': _num(row.get('Frecuencia cardiaca media', '')),
        'maxHR': _num(row.get('FC máxima', '')),
        'averageRunningCadenceInStepsPerMinute': _num(row.get('Cadencia de carrera media', '')),
        'maxRunningCadenceInStepsPerMinute': _num(row.get('Cadencia de carrera máxima', '')),
        'steps': int(_num(row.get('Pasos', '')) or 0),
        'source': 'csv_import',
    }


def _regenerate_ai_only(user_id: str, user_name: str):
    """Generates AI prescription from existing stored data — no Garmin API call."""
    from garmin_onboarding import refresh_start, refresh_progress, refresh_done, refresh_error
    from ai_advisor import generate_daily_recommendation
    refresh_start(user_id, "Analizando actividades importadas...", "day")
    try:
        raw_data = _load_data(user_id)
        if not raw_data:
            refresh_error(user_id, "No hay datos disponibles.")
            return
        refresh_progress(user_id, 50, "Generando prescripción del día...")
        goal = _get_goal(user_id)
        coaching_rules = firestore_helper.get_coaching_rules()
        t_plan = _get_training_plan(user_id)
        try:
            from weekly_summarizer import compute_weekly_summaries
            weekly_summaries = compute_weekly_summaries(raw_data)
            firestore_helper.save_weekly_summaries(user_id, weekly_summaries)
        except Exception as e:
            weekly_summaries = {}
            logger.error(f"Weekly summary error in _regenerate_ai_only for {user_id}: {e}")
        dashboard_data = process_dashboard_data(raw_data, training_goal=goal)
        ai_html = generate_daily_recommendation(
            dashboard_data, user_name=user_name,
            training_goal=goal, coaching_rules=coaching_rules, training_plan=t_plan,
            raw_data=raw_data, weekly_summaries=weekly_summaries
        )
        if ai_html:
            raw_data.setdefault("metadata", {})
            raw_data["metadata"]["ai_recommendation"] = ai_html
            raw_data["metadata"]["ai_recommendation_date"] = now_cdmx().isoformat()
            _save_data(user_id, raw_data)
        refresh_progress(user_id, 100, "¡Prescripción lista!")
        refresh_done(user_id)
    except Exception as e:
        refresh_error(user_id, f"Error al generar prescripción: {str(e)}")
        logger.error(f"AI regen failed for {user_id}: {e}")


def do_refresh(user_id: str, user_name: str = "Atleta", training_goal: dict | None = None) -> tuple:
    """
    Core refresh logic: fetches current month from Garmin, generates AI recommendation,
    saves to GCS and Firestore. Returns a Flask response tuple.
    Extracted here so both /refresh-today and the admin force-refresh can call it.
    """
    from export_data import init_api, fetch_data_current_month
    from ai_advisor import generate_daily_recommendation

    tmp_dir = tempfile.mkdtemp()
    try:
        gcs = _gcs()
        token_prefix = f"users/{user_id}/tokens/"
        gcs.download_directory(token_prefix, tmp_dir)

        api = init_api(token_dir=tmp_dir)
        if not api:
            return jsonify({"error": "No se pudo conectar a Garmin. El token puede haber expirado."}), 500

        # Refresh tokens back to GCS in case they were silently rotated
        gcs.upload_directory(tmp_dir, token_prefix)

        existing_data = _load_data(user_id)
        new_data = fetch_data_current_month(api, existing_data)

        try:
            from weekly_summarizer import compute_weekly_summaries
            weekly_summaries = compute_weekly_summaries(new_data)
            firestore_helper.save_weekly_summaries(user_id, weekly_summaries)
        except Exception as e:
            weekly_summaries = {}
            logger.error(f"Weekly summary error during refresh for {user_id}: {e}")

        try:
            goal = training_goal or _get_goal(user_id)
            coaching_rules = firestore_helper.get_coaching_rules()
            t_plan = _get_training_plan(user_id)
            dashboard_data = process_dashboard_data(new_data, training_goal=goal)
            ai_html = generate_daily_recommendation(
                dashboard_data, user_name=user_name, training_goal=goal,
                coaching_rules=coaching_rules, training_plan=t_plan,
                raw_data=new_data, weekly_summaries=weekly_summaries
            )
            if ai_html:
                if "metadata" not in new_data:
                    new_data["metadata"] = {}
                new_data["metadata"]["ai_recommendation"] = ai_html
                new_data["metadata"]["ai_recommendation_date"] = now_cdmx().isoformat()
        except Exception as e:
            logger.error(f"AI recommendation error during refresh for {user_id}: {e}")

        _save_data(user_id, new_data)
        firestore_helper.upsert_user(user_id, {'last_refresh': now_cdmx().isoformat()})

        ai_rec = new_data.get("metadata", {}).get("ai_recommendation", "")
        return jsonify({"status": "success", "message": "Datos actualizados.", "ai_recommendation": ai_rec}), 200

    except Exception as e:
        logger.error(f"Refresh error for {user_id}: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _count_today_activities(data: dict | None, today_str: str) -> int:
    """Returns the number of activities logged today in the given data dict."""
    if not data:
        return 0
    month_key = today_str[:7]
    acts = data.get("months", {}).get(month_key, {}).get("activities", [])
    return sum(1 for a in acts if a.get("startTimeLocal", "")[:10] == today_str)


def _refresh_background(user_id: str, user_name: str):
    """Runs a Garmin incremental refresh in a background thread (no Flask context needed)."""
    from garmin_onboarding import is_refreshing, refresh_start, refresh_progress, refresh_done, refresh_error
    if is_refreshing(user_id):
        logger.info(f"Auto-refresh skipped for {user_id}: already running.")
        return
    from export_data import init_api, fetch_data_current_month
    from ai_advisor import generate_daily_recommendation
    tmp_dir = tempfile.mkdtemp()
    refresh_start(user_id, "Actualizando datos del día...", "day")
    try:
        gcs = _gcs()
        token_prefix = f"users/{user_id}/tokens/"
        gcs.download_directory(token_prefix, tmp_dir)
        api = init_api(token_dir=tmp_dir)
        if not api:
            gcs.delete_directory(token_prefix)
            firestore_helper.upsert_user(user_id, {'needs_garmin_reconnect': True})
            refresh_error(user_id, "No se pudo conectar con Garmin. Por favor reconecta tu cuenta.")
            logger.warning(f"Auto-refresh: persistent 429 for {user_id} — flagged for reconnect.")
            return
        gcs.upload_directory(tmp_dir, token_prefix)
        refresh_progress(user_id, 30, "Descargando datos del día...")
        existing_data = _load_data(user_id)
        today_str = now_cdmx().date().isoformat()
        acts_before = _count_today_activities(existing_data, today_str)
        has_existing_ai = bool(existing_data and existing_data.get("metadata", {}).get("ai_recommendation"))
        prescription_date = (existing_data or {}).get("metadata", {}).get("ai_recommendation_date", "")[:10]
        prescription_is_today = prescription_date == today_str
        new_data = fetch_data_current_month(api, existing_data)
        acts_after = _count_today_activities(new_data, today_str)
        new_activities_today = acts_after > acts_before

        # Regenerate AI only if: no prescription yet, OR prescription is from a previous day, OR new activities appeared today
        needs_ai = not has_existing_ai or not prescription_is_today or new_activities_today

        try:
            from weekly_summarizer import compute_weekly_summaries
            weekly_summaries = compute_weekly_summaries(new_data)
            firestore_helper.save_weekly_summaries(user_id, weekly_summaries)
        except Exception as e:
            weekly_summaries = {}
            logger.error(f"Weekly summary error during auto-refresh for {user_id}: {e}")

        if needs_ai:
            refresh_progress(user_id, 80, "Generando prescripción del día...")
            try:
                goal = _get_goal(user_id)
                coaching_rules = firestore_helper.get_coaching_rules()
                t_plan = _get_training_plan(user_id)
                dashboard_data = process_dashboard_data(new_data, training_goal=goal)
                ai_html = generate_daily_recommendation(
                    dashboard_data, user_name=user_name, training_goal=goal,
                    coaching_rules=coaching_rules, training_plan=t_plan,
                    raw_data=new_data, weekly_summaries=weekly_summaries
                )
                if ai_html:
                    if "metadata" not in new_data:
                        new_data["metadata"] = {}
                    new_data["metadata"]["ai_recommendation"] = ai_html
                    new_data["metadata"]["ai_recommendation_date"] = now_cdmx().isoformat()
            except Exception as e:
                logger.error(f"AI error during auto-refresh for {user_id}: {e}")
        else:
            refresh_progress(user_id, 80, "Prescripción del día ya está al día.")
            logger.info(f"Auto-refresh: prescription is current for {user_id} (date={prescription_date}, new_acts={new_activities_today}), skipping AI.")
            # Preserve existing AI recommendation in new_data
            if "metadata" not in new_data:
                new_data["metadata"] = {}
            new_data["metadata"]["ai_recommendation"] = existing_data["metadata"]["ai_recommendation"]
            new_data["metadata"]["ai_recommendation_date"] = existing_data["metadata"].get("ai_recommendation_date", "")
        _save_data(user_id, new_data)
        firestore_helper.upsert_user(user_id, {'last_refresh': now_cdmx().isoformat()})
        refresh_done(user_id, "Datos del día actualizados.")
        logger.info(f"Auto-refresh completed for {user_id}")
    except Exception as e:
        refresh_error(user_id, f"Error: {str(e)}")
        logger.error(f"Auto-refresh failed for {user_id}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dashboard_bp.route('/')
@login_required
def index():
    from garmin_onboarding import is_refreshing, refresh_pending
    user_id = session['user_id']
    user_name = session.get('display_name', 'Atleta').split()[0]

    # Check Firestore flags
    user_doc_pre = firestore_helper.get_user(user_id) or {}
    needs_reconnect = user_doc_pre.get('needs_garmin_reconnect', False)
    garmin_sync_disabled = user_doc_pre.get('garmin_sync_disabled', False)

    # Always refresh on a fresh login; also wait if a refresh is already running
    login_refresh = session.pop('needs_login_refresh', False)
    raw_data = _load_data(user_id)
    has_data = bool(raw_data)
    has_ai = bool(raw_data and raw_data.get("metadata", {}).get("ai_recommendation"))

    # ?cached=1 allows skipping the refresh when Garmin is unreachable but user has data
    view_cached = request.args.get('cached') == '1' and has_data

    # If Garmin sync is disabled by admin, show dashboard with CSV import banner (no Garmin call)
    if garmin_sync_disabled:
        session.pop('needs_login_refresh', None)
        # If an AI regen is running (e.g. triggered by CSV import), show loading screen
        if is_refreshing(user_id):
            return render_template('dashboard_loading.html', user_name=user_name)
        if has_data:
            goal = user_doc_pre.get('training_goal')
            user_tz = session.get('timezone')
            dashboard_data = process_dashboard_data(raw_data, training_goal=goal, user_tz=user_tz)
            dashboard_data['last_refresh'] = user_doc_pre.get('last_refresh', '')
            dashboard_data['garmin_sync_disabled'] = True
            return render_template('fitness_report.html', **dashboard_data)
        # No data at all + sync disabled → render with minimal skeleton so all template vars exist
        user_tz = session.get('timezone')
        _today = today_tz(user_tz)
        _mk = _today.strftime('%Y-%m')
        _empty_raw = {'months': {_mk: {'activities': [], 'daily_stats': {}}}, 'metadata': {}, 'user_profile': {}}
        dashboard_data = process_dashboard_data(_empty_raw, training_goal=None, user_tz=user_tz)
        dashboard_data['last_refresh'] = ''
        dashboard_data['garmin_sync_disabled'] = True
        return render_template('fitness_report.html', **dashboard_data)

    # If reconnect is needed but user has cached data, show dashboard with a warning banner
    if needs_reconnect and has_data:
        session.pop('needs_login_refresh', None)
        goal = user_doc_pre.get('training_goal')
        user_tz = session.get('timezone')
        dashboard_data = process_dashboard_data(raw_data, training_goal=goal, user_tz=user_tz)
        dashboard_data['last_refresh'] = user_doc_pre.get('last_refresh', '')
        dashboard_data['garmin_reconnect_needed'] = True
        return render_template('fitness_report.html', **dashboard_data)

    # If reconnect is needed and there's no data at all, go to onboarding
    if needs_reconnect and not has_data:
        session.pop('garmin_connected', None)
        return redirect(url_for('onboarding.warning'))

    if not view_cached and (login_refresh or not has_data or not has_ai or is_refreshing(user_id)):
        if not is_refreshing(user_id):
            # Reset pct BEFORE thread starts so status endpoint won't see stale pct=100
            refresh_pending(user_id)
            threading.Thread(target=_refresh_background, args=(user_id, user_name), daemon=True).start()
        return render_template('dashboard_loading.html', user_name=user_name)

    goal = user_doc_pre.get('training_goal')
    user_tz = session.get('timezone')
    dashboard_data = process_dashboard_data(raw_data, training_goal=goal, user_tz=user_tz)
    dashboard_data['last_refresh'] = user_doc_pre.get('last_refresh', '')
    return render_template('fitness_report.html', **dashboard_data)


@dashboard_bp.route('/prescription/status')
@login_required
def prescription_status():
    """Polls refresh progress and whether today's data is ready."""
    from garmin_onboarding import is_refreshing, get_fetch_progress, refresh_pending
    user_id = session['user_id']
    refreshing = is_refreshing(user_id)
    progress = get_fetch_progress(user_id)
    pct = progress.get("pct", 0)

    # pct==0 means the instance restarted and lost in-memory state mid-refresh.
    # Restart the background thread automatically so the user isn't stuck forever.
    if not refreshing and pct == 0:
        user_name = session.get('display_name', 'Atleta').split()[0]
        user_doc = firestore_helper.get_user(user_id) or {}
        refresh_pending(user_id)
        if user_doc.get('garmin_sync_disabled'):
            # Garmin disabled — only regen AI from existing data, no API call
            threading.Thread(target=_regenerate_ai_only, args=(user_id, user_name), daemon=True).start()
        else:
            threading.Thread(target=_refresh_background, args=(user_id, user_name), daemon=True).start()
        return jsonify({"ready": False, "refreshing": True, "pct": 1, "msg": "Generando prescripción…"})

    # ready=True only when thread explicitly finished (pct==100) AND data has AI rec
    # pct==1 means "pending, thread not yet started" — never ready
    ready = False
    if not refreshing and pct == 100:
        raw_data = _load_data(user_id)
        ready = bool(raw_data and raw_data.get("metadata", {}).get("ai_recommendation"))

    # Include needs_reconnect flag so the loading screen can offer the reconnect button
    needs_reconnect = False
    if pct == -1:
        user_doc = firestore_helper.get_user(user_id) or {}
        needs_reconnect = bool(user_doc.get('needs_garmin_reconnect'))

    return jsonify({
        "ready": ready,
        "refreshing": refreshing,
        "pct": pct,
        "msg": progress.get("msg", "Actualizando datos…"),
        "needs_reconnect": needs_reconnect,
    })


@dashboard_bp.route('/upload-activities', methods=['POST'])
@login_required
def upload_activities():
    """Accepts a Garmin CSV export, merges activities into stored data, triggers AI regen."""
    from garmin_onboarding import refresh_pending
    user_id = session['user_id']
    user_name = session.get('display_name', 'Atleta').split()[0]

    if 'file' not in request.files:
        return jsonify({'error': 'No se recibió archivo'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.csv'):
        return jsonify({'error': 'Solo se aceptan archivos CSV exportados desde Garmin'}), 400
    if len(f.read()) > 10 * 1024 * 1024:
        return jsonify({'error': 'El archivo no debe superar 10 MB'}), 400
    f.seek(0)

    try:
        content = f.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        f.seek(0)
        content = f.read().decode('latin-1')

    reader = csv.DictReader(io.StringIO(content))
    existing_data = _load_data(user_id) or {'months': {}, 'metadata': {}}

    added = 0
    today_str = now_cdmx().date().isoformat()
    for row in reader:
        act = _parse_csv_row(row)
        if not act:
            continue
        month_key = act['startTimeLocal'][:7]
        existing_data['months'].setdefault(month_key, {'activities': [], 'daily_stats': []})
        existing_times = {a['startTimeLocal'] for a in existing_data['months'][month_key].get('activities', [])}
        if act['startTimeLocal'] not in existing_times:
            existing_data['months'][month_key]['activities'].append(act)
            added += 1

    # Always regenerate AI — user explicitly requested a refresh by uploading CSV
    existing_data.setdefault('metadata', {})
    existing_data['metadata']['ai_recommendation_date'] = '2000-01-01'
    _save_data(user_id, existing_data)

    refresh_pending(user_id)
    threading.Thread(target=_regenerate_ai_only, args=(user_id, user_name), daemon=True).start()

    msg = f'{added} actividades nuevas importadas.' if added > 0 else 'Actividades ya cargadas.'
    return jsonify({'added': added, 'message': msg, 'redirect': '/'})


@dashboard_bp.route('/garmin-reconnect')
def garmin_reconnect():
    """Clears Garmin session state and redirects to onboarding so user can re-enter credentials."""
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    session.pop('garmin_connected', None)
    session.pop('needs_login_refresh', None)
    return redirect(url_for('onboarding.warning'))


@dashboard_bp.route('/refresh-today')
@login_required
def refresh_today():
    user_id = session['user_id']

    allowed, count = firestore_helper.check_and_increment_refresh_today(user_id)
    max_limit = firestore_helper.get_max_refresh_today()

    if not allowed:
        return jsonify({
            "error": f"Límite de {max_limit} recargas diarias alcanzado. Intenta mañana.",
            "limit": max_limit,
            "used": count,
        }), 429

    user_name = session.get('display_name', 'Atleta').split()[0]
    goal = _get_goal(user_id)
    return do_refresh(user_id, user_name=user_name, training_goal=goal)


@dashboard_bp.route('/goal', methods=['POST'])
@login_required
def save_goal():
    user_id = session['user_id']
    data = request.get_json(silent=True) or {}

    try:
        pace_str = (data.get('target_pace_str') or '5:00').strip()
        parts = pace_str.split(':')
        pace_min = int(parts[0])
        pace_sec = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return jsonify({"error": "Formato de ritmo inválido. Usa MM:SS, ej. 4:30"}), 400

    try:
        weekly_peak = float(data.get('weekly_peak_km') or 40)
        if weekly_peak <= 0 or weekly_peak > 300:
            raise ValueError
    except ValueError:
        return jsonify({"error": "Pico semanal inválido (1–300 km)."}), 400

    goal = {
        "race_type": (data.get('race_type') or 'Carrera').strip()[:60],
        "target_pace_str": f"{pace_min}:{pace_sec:02d}",
        "target_pace_min": pace_min,
        "target_pace_sec": pace_sec,
        "weekly_peak_km": weekly_peak,
        "easy_hr_max": int(data.get('easy_hr_max') or 155),
        "tempo_hr_min": int(data.get('tempo_hr_min') or 155),
        "tempo_hr_max": int(data.get('tempo_hr_max') or 170),
        "interval_hr_min": int(data.get('interval_hr_min') or 170),
        "description": (data.get('description') or '').strip()[:200],
        "event_date": (data.get('event_date') or '').strip()[:10],
        "injuries": (data.get('injuries') or '').strip()[:300],
        "availability_days": data.get('availability_days') or None,
        "availability_hours_week": data.get('availability_hours_week') or None,
        "plan_duration_weeks": data.get('plan_duration_weeks') or None,
        "plan_start_date": (data.get('plan_start_date') or '').strip()[:10],
    }

    firestore_helper.upsert_user(user_id, {'training_goal': goal})
    logger.info(f"Goal saved for {user_id}: {goal}")
    return jsonify({"status": "ok", "goal": goal})


@dashboard_bp.route('/feedback', methods=['POST'])
@login_required
def submit_feedback():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"error": "El mensaje no puede estar vacío."}), 400
    if len(text) > 1000:
        return jsonify({"error": "Máximo 1000 caracteres."}), 400
    firestore_helper.save_feedback(
        uid=session['user_id'],
        email=session.get('email', ''),
        text=text,
    )
    return jsonify({"status": "ok"})


@dashboard_bp.route('/chat/history', methods=['GET'])
@login_required
def get_chat_history():
    chat_type = request.args.get('type', 'dashboard')
    if chat_type not in ('dashboard', 'goal_setup'):
        return jsonify({"error": "Tipo inválido"}), 400
    uid = request.args.get('uid')
    if uid and session.get('is_admin'):
        target_uid = uid
    else:
        target_uid = session['user_id']
    history = firestore_helper.get_chat_history(target_uid, chat_type)
    return jsonify({"history": history})


@dashboard_bp.route('/chat/history/clear', methods=['POST'])
@login_required
def clear_chat_history():
    data = request.get_json(silent=True) or {}
    chat_type = data.get('type', 'dashboard')
    if chat_type not in ('dashboard', 'goal_setup'):
        return jsonify({"error": "Tipo inválido"}), 400
    firestore_helper.clear_chat_history(session['user_id'], chat_type)
    return jsonify({"status": "ok"})


@dashboard_bp.route('/goal/setup')
@login_required
def goal_setup_view():
    from datetime import date as _date, timedelta as _td
    user_id = session['user_id']
    raw_data = _load_data(user_id)
    if not raw_data:
        return redirect(url_for('dashboard.index'))

    cutoff = (today_cdmx() - _td(days=90)).isoformat()
    cutoff_month = cutoff[:7]
    run_acts = []
    for m_key, m_data in raw_data.get("months", {}).items():
        if m_key >= cutoff_month:
            for a in m_data.get("activities", []):
                if ("run" in a.get("activityType", {}).get("typeKey", "")
                        and a.get("startTimeLocal", "")[:10] >= cutoff):
                    run_acts.append(a)
    run_acts.sort(key=lambda x: x.get("startTimeLocal", ""), reverse=True)

    total_run_km_90 = round(sum(a.get("distance", 0) / 1000 for a in run_acts), 1)
    avg_weekly_km = round(total_run_km_90 / 13, 1)

    recent_paces = []
    for a in run_acts[:5]:
        speed = a.get("averageSpeed", 0)
        if speed and speed > 0:
            ps = 1000 / speed
            recent_paces.append(f"{int(ps // 60)}:{int(ps % 60):02d}")

    user_name = session.get('display_name', 'Atleta').split()[0]
    existing_goal = _get_goal(user_id)
    return render_template('goal_setup.html',
                           avg_weekly_km=avg_weekly_km,
                           total_run_km_90=total_run_km_90,
                           run_count_90=len(run_acts),
                           recent_paces=recent_paces,
                           existing_goal=existing_goal,
                           user_name=user_name)


@dashboard_bp.route('/goal/setup/chat', methods=['POST'])
@login_required
def goal_setup_chat_route():
    user_id = session['user_id']
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    history = data.get('history', [])

    if not message:
        return jsonify({"error": "Mensaje vacío."}), 400
    if len(message) > 1000:
        return jsonify({"error": "Mensaje demasiado largo (máx 1000 caracteres)."}), 400

    raw_data = _load_data(user_id)
    if not raw_data:
        return jsonify({"error": "No hay datos disponibles."}), 404

    try:
        from ai_advisor import goal_setup_chat
        user_name = session.get('display_name', 'Atleta').split()[0]
        coaching_rules = firestore_helper.get_coaching_rules()
        weekly_summaries = firestore_helper.get_weekly_summaries(user_id)
        result = goal_setup_chat(message, history, raw_data, user_name=user_name, coaching_rules=coaching_rules, weekly_summaries=weekly_summaries, user_tz=session.get('timezone'))

        updated = list(history) + [
            {"role": "user", "content": message},
            {"role": "model", "content": result.get("reply", "")},
        ]
        firestore_helper.save_chat_history(user_id, 'goal_setup', updated)

        return jsonify(result)
    except Exception as e:
        logger.error(f"Error en /goal/setup/chat para {user_id}: {e}")
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route('/goal/setup/upload-plan', methods=['POST'])
@login_required
def upload_training_plan():
    user_id = session['user_id']

    if 'image' not in request.files:
        return jsonify({"error": "No se recibió ninguna imagen."}), 400

    file = request.files['image']
    if not file or file.filename == '':
        return jsonify({"error": "Archivo vacío."}), 400

    allowed_types = {'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'}
    content_type = file.content_type or 'image/jpeg'
    if content_type not in allowed_types:
        return jsonify({"error": "Formato no soportado. Usa JPG, PNG o WEBP."}), 400

    image_bytes = file.read()
    if len(image_bytes) > 5 * 1024 * 1024:
        return jsonify({"error": "La imagen no puede superar 5 MB."}), 400

    try:
        from ai_advisor import extract_training_plan_from_image
        user_name = session.get('display_name', 'Atleta').split()[0]
        result = extract_training_plan_from_image(image_bytes, content_type, user_name=user_name)

        if 'error' in result:
            return jsonify({"error": result['error']}), 500

        # Store image in GCS
        ext = content_type.split('/')[-1].replace('jpeg', 'jpg')
        image_blob = f"users/{user_id}/training_plan.{ext}"
        _gcs().upload_bytes(image_blob, image_bytes, content_type)

        # Save extracted plan to Firestore
        firestore_helper.upsert_user(user_id, {
            'training_plan': {
                'text': result['text'],
                'summary': result.get('summary', ''),
                'image_path': image_blob,
                'uploaded_at': now_cdmx().isoformat(),
            }
        })

        logger.info(f"Training plan uploaded and extracted for {user_id}")
        return jsonify({"status": "ok", "plan_text": result['text'], "summary": result.get('summary', '')})

    except Exception as e:
        logger.error(f"Error in upload_training_plan for {user_id}: {e}")
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route('/set-timezone', methods=['POST'])
@login_required
def set_timezone():
    """Saves the user's browser-detected IANA timezone to session and Firestore."""
    from tz_utils import _resolve_tz
    data = request.get_json(silent=True) or {}
    tz_str = (data.get('timezone') or '').strip()
    if not tz_str:
        return jsonify({"error": "Missing timezone"}), 400
    # Validate it's a known IANA zone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        return jsonify({"error": "Unknown timezone"}), 400
    session['timezone'] = tz_str
    firestore_helper.upsert_user(session['user_id'], {'timezone': tz_str})
    return jsonify({"status": "ok", "timezone": tz_str})


@dashboard_bp.route('/weekly-summaries')
@login_required
def user_weekly_summaries():
    user_id = session['user_id']
    weeks = firestore_helper.get_weekly_summaries(user_id)
    sorted_keys = sorted(weeks.keys())
    return jsonify({"weeks": [weeks[k] for k in sorted_keys]})


@dashboard_bp.route('/ask-ai', methods=['POST'])
@login_required
def ask_ai():
    user_id = session['user_id']
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    history = data.get('history', [])

    if not question:
        return jsonify({"error": "La pregunta no puede estar vacía."}), 400
    if len(question) > 500:
        return jsonify({"error": "La pregunta no puede superar los 500 caracteres."}), 400

    raw_data = _load_data(user_id)
    if not raw_data:
        return jsonify({"error": "No hay datos disponibles. Recarga primero."}), 404

    try:
        from ai_advisor import ask_ai_with_context
        goal = _get_goal(user_id)
        coaching_rules = firestore_helper.get_coaching_rules()
        t_plan = _get_training_plan(user_id)
        weekly_summaries = firestore_helper.get_weekly_summaries(user_id)
        dashboard_data = process_dashboard_data(raw_data, training_goal=goal)
        user_name = session.get('display_name', 'Atleta').split()[0]
        answer = ask_ai_with_context(question, dashboard_data, raw_data, history=history, user_name=user_name, training_goal=goal, coaching_rules=coaching_rules, training_plan=t_plan, weekly_summaries=weekly_summaries, user_tz=session.get('timezone'))

        updated = list(history) + [
            {"role": "user", "content": question},
            {"role": "model", "content": answer},
        ]
        firestore_helper.save_chat_history(user_id, 'dashboard', updated)

        return jsonify({"response": answer})
    except Exception as e:
        logger.error(f"Error en /ask-ai para {user_id}: {e}")
        return jsonify({"error": str(e)}), 500


# In-memory tracking for async plan generation (similar to garmin_onboarding.py)
_plan_generating: set = set()
_plan_error: dict = {}  # uid → error message


def _generate_plan_background(user_id: str, goal: dict, user_name: str, user_tz: str | None, weekly_summaries: dict):
    """Generates training plan in background thread and saves to Firestore."""
    from ai_advisor import generate_training_plan_schedule
    _plan_generating.add(user_id)
    _plan_error.pop(user_id, None)
    try:
        plan = generate_training_plan_schedule(
            goal, weekly_summaries=weekly_summaries,
            user_name=user_name, user_tz=user_tz
        )
        if plan:
            firestore_helper.upsert_user(user_id, {'training_plan_schedule': plan})
            logger.info(f"Training plan saved for {user_id}: {plan.get('total_weeks')} weeks")
        else:
            _plan_error[user_id] = "No se pudo generar el plan. Intenta de nuevo."
            logger.error(f"Plan generation returned None for {user_id}")
    except Exception as e:
        _plan_error[user_id] = str(e)
        logger.error(f"Plan generation failed for {user_id}: {e}")
    finally:
        _plan_generating.discard(user_id)


@dashboard_bp.route('/plan')
@login_required
def training_plan_view():
    from datetime import date as _date
    user_id = session['user_id']
    generating = user_id in _plan_generating
    user_doc = firestore_helper.get_user(user_id) or {}
    plan = user_doc.get('training_plan_schedule')

    # No plan and not generating → send to goal setup
    if not plan and not generating:
        return redirect(url_for('dashboard.goal_setup_view'))

    goal = user_doc.get('training_goal')
    user_name = session.get('display_name', 'Atleta').split()[0]
    user_tz_str = session.get('timezone')
    today = today_tz(user_tz_str)

    current_week = None
    week_dates = {}  # week_number → {'start': 'lun 31 mar', 'end': 'dom 6 abr'}
    _MONTHS_ES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']
    _DAYS_ES   = ['lun','mar','mié','jue','vie','sáb','dom']

    def _fmt(d):
        return f"{_DAYS_ES[d.weekday()]} {d.day} {_MONTHS_ES[d.month-1]}"

    if plan:
        plan_start_str = plan.get('plan_start_date', '')
        if plan_start_str:
            try:
                from datetime import timedelta as _td
                plan_start = _date.fromisoformat(plan_start_str)
                total_weeks = plan.get('total_weeks', 0)
                for w in range(1, total_weeks + 1):
                    ws = plan_start + _td(weeks=w - 1)
                    we = ws + _td(days=6)
                    week_dates[w] = {'start': _fmt(ws), 'end': _fmt(we)}
                delta = (today - plan_start).days
                if delta >= 0:
                    current_week = min(delta // 7 + 1, total_weeks or 99)
            except (ValueError, TypeError):
                pass

    return render_template('training_plan.html',
                           plan=plan, goal=goal, user_name=user_name,
                           current_week=current_week,
                           week_dates=week_dates,
                           today_str=today.isoformat(),
                           generating=generating)


@dashboard_bp.route('/plan/status')
@login_required
def plan_status():
    user_id = session['user_id']
    generating = user_id in _plan_generating
    error = _plan_error.get(user_id)
    if generating:
        return jsonify({"ready": False, "generating": True})
    if error:
        return jsonify({"ready": False, "generating": False, "error": error})
    user_doc = firestore_helper.get_user(user_id) or {}
    ready = bool(user_doc.get('training_plan_schedule'))
    return jsonify({"ready": ready, "generating": False})


@dashboard_bp.route('/goal/generate-plan', methods=['POST'])
@login_required
def generate_plan_route():
    user_id = session['user_id']
    if user_id in _plan_generating:
        return jsonify({"status": "generating", "redirect": "/plan"})
    user_doc = firestore_helper.get_user(user_id) or {}
    goal = user_doc.get('training_goal')
    if not goal:
        return jsonify({"error": "No hay objetivo configurado. Primero define tu objetivo."}), 400

    # Limit plan generation to once per calendar month per user
    import datetime as _dt
    user_tz = session.get('timezone')
    existing_plan = user_doc.get('training_plan_schedule') or {}
    generated_at = existing_plan.get('generated_at', '')
    this_month = today_tz(user_tz).strftime('%Y-%m')
    if generated_at and generated_at[:7] == this_month:
        today_date = today_tz(user_tz)
        next_month = (today_date.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        days_left = (next_month - today_date).days
        return jsonify({
            "error": f"Ya generaste tu plan este mes (el {generated_at[:10]}). Podrás regenerarlo en {days_left} días.",
            "limit_reached": True
        }), 429

    weekly_summaries = firestore_helper.get_weekly_summaries(user_id)
    user_name = session.get('display_name', 'Atleta').split()[0]
    threading.Thread(
        target=_generate_plan_background,
        args=(user_id, goal, user_name, user_tz, weekly_summaries),
        daemon=True
    ).start()
    return jsonify({"status": "ok", "redirect": "/plan"})
