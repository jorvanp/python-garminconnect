import csv
import datetime as _dt
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


def _get_regen_status(user_doc: dict, user_tz: str | None) -> tuple[bool, str]:
    """Returns (can_regenerate, next_regen_date_str). Premium users can always regenerate."""
    if user_doc.get('is_premium'):
        return True, ''
    plan = user_doc.get('training_plan_schedule') or {}
    generated_at = plan.get('generated_at', '')
    this_month = today_tz(user_tz).strftime('%Y-%m')
    if generated_at and generated_at[:7] == this_month:
        today_d = today_tz(user_tz)
        next_month = (today_d.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        return False, next_month.strftime('%d/%m/%Y')
    return True, ''


_ACTIVITY_TYPE_MAP = {
    'Correr': {'typeId': 1, 'typeKey': 'running'},
    'Carrera': {'typeId': 1, 'typeKey': 'running'},
    'Entrenamiento en cinta': {'typeId': 1, 'typeKey': 'treadmill_running'},
    'Carrera en cinta': {'typeId': 1, 'typeKey': 'treadmill_running'},
    'Trail running': {'typeId': 1, 'typeKey': 'trail_running'},
    'Carrera de trail': {'typeId': 1, 'typeKey': 'trail_running'},
    'Virtual Running': {'typeId': 1, 'typeKey': 'virtual_run'},
    'Ciclismo': {'typeId': 2, 'typeKey': 'cycling'},
    'Ciclismo en ruta': {'typeId': 2, 'typeKey': 'cycling'},
    'Ciclismo indoor': {'typeId': 25, 'typeKey': 'indoor_cycling'},
    'Ciclismo en sala': {'typeId': 25, 'typeKey': 'indoor_cycling'},
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


def _recompute_summaries_only(user_id: str):
    """Recomputes weekly summaries from existing stored data — no Garmin API, no AI call."""
    from garmin_onboarding import refresh_start, refresh_done, refresh_error
    refresh_start(user_id, "Procesando actividades importadas...", "day")
    try:
        raw_data = _load_data(user_id)
        if not raw_data:
            refresh_error(user_id, "No hay datos disponibles.")
            return
        try:
            from weekly_summarizer import compute_weekly_summaries
            summaries = compute_weekly_summaries(raw_data)
            firestore_helper.save_weekly_summaries(user_id, summaries)
        except Exception as e:
            logger.error(f"Weekly summary error in _recompute_summaries_only for {user_id}: {e}")
        refresh_done(user_id, "Actividades cargadas.")
    except Exception as e:
        refresh_error(user_id, f"Error: {str(e)}")
        logger.error(f"Recompute summaries failed for {user_id}: {e}")


def do_refresh(user_id: str, user_name: str = "Atleta", training_goal: dict | None = None) -> tuple:
    """
    Core refresh logic: fetches current month from Garmin, generates AI recommendation,
    saves to GCS and Firestore. Returns a Flask response tuple.
    Extracted here so both /refresh-today and the admin force-refresh can call it.
    """
    from export_data import init_api, fetch_data_current_month

    tmp_dir = tempfile.mkdtemp()
    try:
        gcs = _gcs()
        token_prefix = f"users/{user_id}/tokens/"
        gcs.download_directory(token_prefix, tmp_dir)

        api = init_api(token_dir=tmp_dir)
        if not api:
            return jsonify({"error": "No se pudo conectar a Garmin. El token puede haber expirado."}), 500

        gcs.upload_directory(tmp_dir, token_prefix)

        existing_data = _load_data(user_id)
        new_data = fetch_data_current_month(api, existing_data)

        try:
            from weekly_summarizer import compute_weekly_summaries
            summaries = compute_weekly_summaries(new_data)
            firestore_helper.save_weekly_summaries(user_id, summaries)
        except Exception as e:
            logger.error(f"Weekly summary error during refresh for {user_id}: {e}")

        _save_data(user_id, new_data)
        firestore_helper.upsert_user(user_id, {'last_refresh': now_cdmx().isoformat()})
        return jsonify({"status": "success", "message": "Datos actualizados."}), 200

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


def _refresh_background(user_id: str, user_name: str, wait_before_start: int = 0):
    """Runs a Garmin incremental refresh in a background thread (no Flask context needed)."""
    from garmin_onboarding import is_refreshing, refresh_start, refresh_progress, refresh_done, refresh_error
    if is_refreshing(user_id):
        logger.info(f"Auto-refresh skipped for {user_id}: already running.")
        return
    if wait_before_start:
        import time as _time
        logger.info(f"Auto-restart: waiting {wait_before_start}s before Garmin call for {user_id}")
        _time.sleep(wait_before_start)
    from export_data import init_api, fetch_data_current_month
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
        refresh_progress(user_id, 40, "Descargando datos del día...")
        existing_data = _load_data(user_id)
        new_data = fetch_data_current_month(api, existing_data)
        refresh_progress(user_id, 80, "Calculando resúmenes semanales...")
        try:
            from weekly_summarizer import compute_weekly_summaries
            summaries = compute_weekly_summaries(new_data)
            firestore_helper.save_weekly_summaries(user_id, summaries)
        except Exception as e:
            logger.error(f"Weekly summary error during auto-refresh for {user_id}: {e}")
        _save_data(user_id, new_data)
        firestore_helper.upsert_user(user_id, {'last_refresh': now_cdmx().isoformat()})
        refresh_done(user_id, "Datos del día actualizados.")
        logger.info(f"Auto-refresh completed for {user_id}")
    except Exception as e:
        refresh_error(user_id, f"Error: {str(e)}")
        logger.error(f"Auto-refresh failed for {user_id}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _add_sections(dashboard_data: dict, user_doc: dict = None) -> dict:
    """Injects global sections_disabled config into dashboard_data."""
    sections = firestore_helper.get_global_sections()
    sections.setdefault('snapshot', True)
    dashboard_data['sections_disabled'] = sections
    return dashboard_data


_DISTANCE_MAP = [
    ('medio', 21.1), ('media', 21.1), ('half', 21.1), ('21', 21.1),
    ('maratón', 42.2), ('maraton', 42.2), ('marathon', 42.2), ('42', 42.2),
    ('10k', 10.0), ('10 k', 10.0), ('10km', 10.0),
    ('5k', 5.0), ('5 k', 5.0), ('5km', 5.0),
]
_DAYS_ES_LONG = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
_MONTHS_ES_SHORT = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def _add_race_hero(dashboard_data: dict, user_doc: dict, user_tz: str | None) -> dict:
    """Adds race hero card fields to dashboard_data: image, countdown, distance, time goal, mini-plan."""
    from datetime import date as _date_cls, timedelta as _td
    from tz_utils import today_tz as _today_tz

    goal = dashboard_data.get('training_goal', {})

    # --- Image URL and name from joined race ---
    race_id = goal.get('race_id')
    race_image_url = None
    race_display_name = None
    if race_id:
        try:
            race_doc = firestore_helper.get_race(race_id)
            if race_doc:
                if race_doc.get('image_url'):
                    race_image_url = race_doc['image_url']
                if race_doc.get('name'):
                    race_display_name = race_doc['name']
        except Exception:
            pass
    dashboard_data['race_image_url'] = race_image_url
    dashboard_data['race_display_name'] = race_display_name

    # --- Days until race + formatted date ---
    event_date_str = goal.get('event_date', '')
    days_until_race = None
    event_date_display = ''
    if event_date_str:
        try:
            ed = _date_cls.fromisoformat(event_date_str)
            today = _today_tz(user_tz)
            days_until_race = max(0, (ed - today).days)
            event_date_display = f"{_DAYS_ES_LONG[ed.weekday()]}, {ed.day} {_MONTHS_ES_SHORT[ed.month - 1]} {ed.year}"
        except (ValueError, TypeError):
            pass
    dashboard_data['days_until_race'] = days_until_race
    dashboard_data['event_date_display'] = event_date_display

    # --- Race distance + target finish time ---
    race_type_lower = goal.get('race_type', '').lower()
    race_distance_km = None
    for key, val in _DISTANCE_MAP:
        if key in race_type_lower:
            race_distance_km = val
            break
    target_time_str = ''
    if race_distance_km:
        pace_min = goal.get('target_pace_min')
        pace_sec = goal.get('target_pace_sec', 0) or 0
        if pace_min:
            total_mins = (int(pace_min) + int(pace_sec) / 60.0) * race_distance_km
            h = int(total_mins // 60)
            m = int(total_mins % 60)
            target_time_str = f"Sub {h}h {m:02d}min" if h else f"Sub {m}min"
    dashboard_data['race_distance_km'] = race_distance_km
    dashboard_data['target_time_str'] = target_time_str

    # --- Current week mini-plan from training_plan_schedule ---
    plan_current_week_workouts = None
    plan_current_week_num = None
    plan_total_weeks = None
    plan_schedule = (user_doc or {}).get('training_plan_schedule')
    if plan_schedule:
        try:
            plan_start_str = plan_schedule.get('plan_start_date', '')
            total_weeks = plan_schedule.get('total_weeks', 0)
            plan_total_weeks = total_weeks
            if plan_start_str:
                today = _today_tz(user_tz)
                plan_start = _date_cls.fromisoformat(plan_start_str)
                delta = (today - plan_start).days
                if delta >= 0:
                    w_num = min(delta // 7 + 1, total_weeks or 99)
                    plan_current_week_num = w_num
                    for week in plan_schedule.get('weeks', []):
                        if week.get('week_number') == w_num:
                            plan_current_week_workouts = week.get('workouts', [])
                            break
        except (ValueError, TypeError):
            pass
    # --- Match actual activities to each day of the current week ---
    _DAY_OFFSETS = {
        'lunes': 0, 'martes': 1, 'miércoles': 2, 'miercoles': 2,
        'jueves': 3, 'viernes': 4, 'sábado': 5, 'sabado': 5, 'domingo': 6,
    }
    _ACT_ICONS = {
        'running': '🏃', 'trail_running': '🏔', 'cycling': '🚴',
        'indoor_cycling': '🚴', 'mountain_biking': '🚵', 'strength_training': '💪',
        'swimming': '🏊', 'open_water_swimming': '🏊', 'walking': '🚶',
        'yoga': '🧘', 'fitness_equipment': '🏋', 'elliptical': '🔄',
        'cardio': '🤸', 'hiking': '🥾', 'rowing': '🚣',
    }
    def _act_icon(type_key: str) -> str:
        tk = (type_key or '').lower()
        for k, v in _ACT_ICONS.items():
            if k in tk:
                return v
        return '⚡'

    def _build_act_detail(act: dict) -> dict:
        tk = (act.get('activityType', {}).get('typeKey') or '').lower()
        km = round(act.get('distance', 0) / 1000.0, 1) if act.get('distance') else None
        spd = act.get('averageSpeed', 0) or 0
        pace = None
        if spd > 0 and 'run' in tk:
            ps = 1000 / spd
            pace = f"{int(ps // 60)}:{int(ps % 60):02d}"
        dur_sec = act.get('duration', 0) or 0
        dur_min = int(dur_sec / 60) if dur_sec else None
        name = (act.get('activityName') or tk.replace('_', ' ').title() or 'Actividad').strip()
        return {'type_key': tk, 'icon': _act_icon(tk), 'km': km,
                'pace': pace, 'duration_min': dur_min, 'name': name}

    week_has_actuals = False
    if plan_current_week_workouts and plan_start_str:
        try:
            plan_start = _date_cls.fromisoformat(plan_start_str)
            plan_week_start = plan_start + _td(weeks=plan_current_week_num - 1)
            today = _today_tz(user_tz)

            acts_by_date: dict = {}
            for act in (dashboard_data.get('recent_activities') or []):
                d = (act.get('startTimeLocal') or '')[:10]
                if d:
                    acts_by_date.setdefault(d, []).append(act)

            for w in plan_current_week_workouts:
                day_name = (w.get('day_name') or '').lower().strip()
                offset = _DAY_OFFSETS.get(day_name)
                w['_acts'] = []
                w['_primary_act'] = None
                w['_actual_km'] = None
                w['_status'] = None
                if offset is None:
                    continue
                day_date = plan_week_start + _td(days=offset)
                if day_date > today:
                    continue
                raw_acts = acts_by_date.get(day_date.isoformat(), [])
                if not raw_acts:
                    continue

                week_has_actuals = True
                details = [_build_act_detail(a) for a in raw_acts]
                w['_acts'] = details

                # Primary: prefer running for run days, then first activity
                planned_type = (w.get('type') or '').lower()
                run_acts = [d for d in details if 'run' in d['type_key']]
                strength_acts = [d for d in details if 'strength' in d['type_key']]
                if 'cross' in planned_type or 'strength' in planned_type:
                    primary = (strength_acts or details)[0]
                elif run_acts:
                    primary = run_acts[0]
                else:
                    primary = details[0]
                w['_primary_act'] = primary

                # Status based on primary running km vs planned km
                planned_km = float(w.get('km') or 0)
                if planned_km > 0 and primary.get('km'):
                    ratio = primary['km'] / planned_km
                    w['_actual_km'] = primary['km']
                    w['_status'] = 'green' if ratio >= 0.9 else ('yellow' if ratio >= 0.7 else 'white')
                elif primary.get('km'):
                    w['_actual_km'] = primary['km']
                    w['_status'] = 'white'
                else:
                    w['_status'] = 'green'  # strength/cross with no distance = completed
        except Exception:
            pass

    dashboard_data['plan_current_week_workouts'] = plan_current_week_workouts
    dashboard_data['plan_current_week_num'] = plan_current_week_num
    dashboard_data['plan_total_weeks'] = plan_total_weeks
    dashboard_data['has_plan_schedule'] = bool(plan_schedule)
    dashboard_data['week_has_actuals'] = week_has_actuals

    # If plan exists but goal_configured is False (e.g. goal save failed),
    # fill goal fields from the plan's metadata so the hero card renders correctly.
    if plan_schedule and not dashboard_data.get('goal_configured'):
        goal = dashboard_data.get('training_goal', {}) or {}
        if not goal.get('target_pace_str') and plan_schedule.get('goal_pace'):
            goal['target_pace_str'] = plan_schedule['goal_pace']
        if not goal.get('race_type') and plan_schedule.get('title'):
            goal['race_type'] = plan_schedule['title']
        if not goal.get('event_date') and plan_schedule.get('event_date'):
            goal['event_date'] = plan_schedule['event_date']
        dashboard_data['training_goal'] = goal
        dashboard_data['goal_configured'] = True

    return dashboard_data


@dashboard_bp.route('/races/<race_id>/image')
def race_image_proxy(race_id: str):
    from flask import Response as _Resp
    from gcs_helper import GCSHelper as _GCS
    race = firestore_helper.get_race(race_id)
    if not race or not race.get('image_url'):
        abort(404)
    bucket = os.environ.get('GARMIN_BUCKET', 'garmin-dashboard-data')
    gcs = _GCS(bucket)
    data = gcs.download_bytes(f"race_images/{race_id}")
    if not data:
        abort(404)
    return _Resp(data, content_type=race.get('image_content_type', 'image/jpeg'))


@dashboard_bp.route('/')
def index():
    # Unauthenticated users see the landing page
    if 'user_id' not in session:
        return render_template('home.html')
    # Profile assessment: check once per session
    if not session.get('has_profile_assessment'):
        if firestore_helper.get_latest_assessment(session['user_id']):
            session['has_profile_assessment'] = True
        else:
            return redirect(url_for('onboarding.profile_setup'))
    if not session.get('garmin_connected'):
        return redirect(url_for('onboarding.warning'))
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
            if dashboard_data:
                dashboard_data['last_refresh'] = user_doc_pre.get('last_refresh', '')
                dashboard_data['garmin_sync_disabled'] = True
                _add_race_hero(dashboard_data, user_doc_pre, user_tz)
                _add_sections(dashboard_data, user_doc_pre)
                return render_template('fitness_report.html', **dashboard_data)
        # No data at all + sync disabled → render with minimal skeleton so all template vars exist
        user_tz = session.get('timezone')
        _today = today_tz(user_tz)
        _mk = _today.strftime('%Y-%m')
        _empty_raw = {'months': {_mk: {'activities': [], 'daily_stats': {}}}, 'metadata': {}, 'user_profile': {}}
        dashboard_data = process_dashboard_data(_empty_raw, training_goal=None, user_tz=user_tz)
        dashboard_data['last_refresh'] = ''
        dashboard_data['garmin_sync_disabled'] = True
        _add_race_hero(dashboard_data, user_doc_pre, user_tz)
        _add_sections(dashboard_data, user_doc_pre)
        return render_template('fitness_report.html', **dashboard_data)

    # If reconnect is needed but user has cached data, show dashboard with a warning banner
    if needs_reconnect and has_data:
        session.pop('needs_login_refresh', None)
        goal = user_doc_pre.get('training_goal')
        user_tz = session.get('timezone')
        dashboard_data = process_dashboard_data(raw_data, training_goal=goal, user_tz=user_tz)
        if dashboard_data:
            dashboard_data['last_refresh'] = user_doc_pre.get('last_refresh', '')
            dashboard_data['garmin_reconnect_needed'] = True
            _add_race_hero(dashboard_data, user_doc_pre, user_tz)
            _add_sections(dashboard_data, user_doc_pre)
            return render_template('fitness_report.html', **dashboard_data)

    # If reconnect is needed and there's no data at all, go to onboarding
    if needs_reconnect and not has_data:
        session.pop('garmin_connected', None)
        return redirect(url_for('onboarding.warning'))

    if not view_cached and (login_refresh or not has_data or is_refreshing(user_id)):
        if not is_refreshing(user_id):
            # Reset pct BEFORE thread starts so status endpoint won't see stale pct=100
            refresh_pending(user_id)
            threading.Thread(target=_refresh_background, args=(user_id, user_name), daemon=True).start()
        return render_template('dashboard_loading.html', user_name=user_name)

    goal = user_doc_pre.get('training_goal')
    user_tz = session.get('timezone')
    dashboard_data = process_dashboard_data(raw_data, training_goal=goal, user_tz=user_tz)
    if not dashboard_data:
        # months dict is empty (fetch in progress or incomplete) — show loading screen
        return render_template('dashboard_loading.html', user_name=user_name)
    dashboard_data['last_refresh'] = user_doc_pre.get('last_refresh', '')
    _add_race_hero(dashboard_data, user_doc_pre, user_tz)
    _add_sections(dashboard_data, user_doc_pre)
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
            threading.Thread(target=_recompute_summaries_only, args=(user_id,), daemon=True).start()
        else:
            # Wait 15s before calling Garmin — gives the previous instance time to die
            # and avoids the simultaneous-call 429 that happens during rolling deploys.
            threading.Thread(target=_refresh_background,
                             args=(user_id, user_name),
                             kwargs={'wait_before_start': 15},
                             daemon=True).start()
        return jsonify({"ready": False, "refreshing": True, "pct": 1,
                        "msg": "Reconectando tras reinicio del servidor…"})

    # ready=True when thread finished (pct==100) AND user has data
    ready = False
    if not refreshing and pct == 100:
        raw_data = _load_data(user_id)
        ready = bool(raw_data)

    # Include needs_reconnect and garmin_blocked flags for the loading screen
    needs_reconnect = False
    garmin_blocked = False
    if pct == -1:
        user_doc = firestore_helper.get_user(user_id) or {}
        needs_reconnect = bool(user_doc.get('needs_garmin_reconnect'))
        garmin_blocked = bool(progress.get("garmin_blocked") or user_doc.get('garmin_sync_disabled'))

    return jsonify({
        "ready": ready,
        "refreshing": refreshing,
        "pct": pct,
        "msg": progress.get("msg", "Actualizando datos…"),
        "needs_reconnect": needs_reconnect,
        "garmin_blocked": garmin_blocked,
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
    updated = 0
    today_str = now_cdmx().date().isoformat()
    for row in reader:
        act = _parse_csv_row(row)
        if not act:
            continue
        month_key = act['startTimeLocal'][:7]
        existing_data['months'].setdefault(month_key, {'activities': [], 'daily_stats': {}})
        month_acts = existing_data['months'][month_key].get('activities', [])
        existing_idx = next((i for i, a in enumerate(month_acts) if a['startTimeLocal'] == act['startTimeLocal']), None)
        if existing_idx is None:
            month_acts.append(act)
            added += 1
        else:
            # Upsert: update typeKey if it was previously stored as 'other' and now correctly mapped
            old_type = month_acts[existing_idx].get('activityType', {}).get('typeKey', 'other')
            new_type = act.get('activityType', {}).get('typeKey', 'other')
            if old_type == 'other' and new_type != 'other':
                month_acts[existing_idx]['activityType'] = act['activityType']
                updated += 1

    existing_data.setdefault('metadata', {})
    _save_data(user_id, existing_data)
    try:
        from weekly_summarizer import compute_weekly_summaries
        firestore_helper.save_weekly_summaries(user_id, compute_weekly_summaries(existing_data))
    except Exception as e:
        logger.error(f"Weekly summary error after CSV upload for {user_id}: {e}")

    if added > 0 and updated > 0:
        msg = f'{added} actividades nuevas importadas, {updated} corregidas.'
    elif added > 0:
        msg = f'{added} actividades nuevas importadas.'
    elif updated > 0:
        msg = f'{updated} actividades corregidas.'
    else:
        msg = 'Actividades ya cargadas (sin cambios).'
    return jsonify({'added': added, 'updated': updated, 'message': msg, 'redirect': '/'})


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
        import re as _re
        pace_raw = (data.get('target_pace_str') or '5:00').strip()
        # Normalize: if a range like "6:00-6:30" take the first value
        pace_raw = _re.split(r'\s*[-–]\s*', pace_raw)[0]
        # Strip trailing units: " min/km", "/km", " min", etc.
        pace_raw = _re.sub(r'\s*(min/km|/km|min|km)\s*$', '', pace_raw, flags=_re.IGNORECASE).strip()
        parts = pace_raw.split(':')
        pace_min = int(parts[0])
        pace_sec = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= pace_min <= 30 and 0 <= pace_sec <= 59):
            raise ValueError
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

    # Return candidate races so the frontend can ask the user to link up
    race_candidates = []
    if goal.get('event_date'):
        raw_candidates = firestore_helper.find_similar_races(
            goal.get('race_type', ''), goal.get('event_date', '')
        )
        for r in raw_candidates:
            participant_names = [p.get('name', '') for p in r.get('participants', {}).values() if p.get('name')]
            race_candidates.append({
                'id': r['id'],
                'name': r.get('name', ''),
                'race_type': r.get('race_type', ''),
                'event_date': r.get('event_date', ''),
                'participant_count': r.get('participant_count', 0),
                'participant_names': participant_names,
            })

    return jsonify({"status": "ok", "goal": goal, "race_candidates": race_candidates})


@dashboard_bp.route('/goal/join-race', methods=['POST'])
@login_required
def join_race():
    user_id = session['user_id']
    data = request.get_json(silent=True) or {}
    race_id = data.get('race_id')
    create_new = data.get('create_new', False)

    user_doc = firestore_helper.get_user(user_id) or {}
    goal = user_doc.get('training_goal', {})

    if not goal.get('event_date'):
        return jsonify({"error": "No hay fecha de evento en el objetivo."}), 400

    participant_data = {
        'name': session.get('display_name', 'Atleta'),
        'email': session.get('email', ''),
        'pace_target': goal.get('target_pace_str', ''),
        'weekly_peak_km': goal.get('weekly_peak_km', 0),
        'race_type': goal.get('race_type', ''),
        'joined_at': now_cdmx().isoformat(),
    }

    if not race_id or create_new:
        name = (goal.get('description') or f"{goal.get('race_type', 'Carrera')} — {goal.get('event_date', '')}").strip()
        race_id = firestore_helper.create_race(
            race_type=goal.get('race_type', ''),
            event_date=goal.get('event_date', ''),
            name=name,
            uid=user_id,
            participant_data=participant_data,
        )
    else:
        firestore_helper.add_participant_to_race(race_id, user_id, participant_data)

    updated_goal = dict(goal)
    updated_goal['race_id'] = race_id
    firestore_helper.upsert_user(user_id, {'training_goal': updated_goal})

    logger.info(f"User {user_id} joined race {race_id} (create_new={create_new})")
    return jsonify({"status": "ok", "race_id": race_id})


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

    run_acts = []
    if raw_data:
        cutoff = (today_cdmx() - _td(days=90)).isoformat()
        cutoff_month = cutoff[:7]
        for m_key, m_data in raw_data.get("months", {}).items():
            if m_key >= cutoff_month:
                for a in m_data.get("activities", []):
                    if ("run" in a.get("activityType", {}).get("typeKey", "")
                            and a.get("startTimeLocal", "")[:10] >= cutoff):
                        run_acts.append(a)
        run_acts.sort(key=lambda x: x.get("startTimeLocal", ""), reverse=True)

    total_run_km_90 = round(sum(a.get("distance", 0) / 1000 for a in run_acts), 1)
    avg_weekly_km = round(total_run_km_90 / 13, 1) if run_acts else 0.0

    recent_paces = []
    for a in run_acts[:5]:
        speed = a.get("averageSpeed", 0)
        if speed and speed > 0:
            ps = 1000 / speed
            recent_paces.append(f"{int(ps // 60)}:{int(ps % 60):02d}")

    user_name = session.get('display_name', 'Atleta').split()[0]
    existing_goal = _get_goal(user_id)

    user_doc_gs = firestore_helper.get_user(user_id) or {}
    is_premium = bool(user_doc_gs.get('is_premium'))
    can_regenerate, next_regen_date = _get_regen_status(user_doc_gs, session.get('timezone'))

    return render_template('goal_setup.html',
                           avg_weekly_km=avg_weekly_km,
                           total_run_km_90=total_run_km_90,
                           run_count_90=len(run_acts),
                           recent_paces=recent_paces,
                           existing_goal=existing_goal,
                           user_name=user_name,
                           can_regenerate=can_regenerate,
                           next_regen_date=next_regen_date,
                           is_premium=is_premium)


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

    raw_data = _load_data(user_id) or {"months": {}, "metadata": {}}

    try:
        from ai_advisor import goal_setup_chat
        from tz_utils import today_tz
        user_name = session.get('display_name', 'Atleta').split()[0]
        coaching_rules = firestore_helper.get_coaching_rules()
        weekly_summaries = firestore_helper.get_weekly_summaries(user_id)
        user_profile = firestore_helper.get_latest_assessment(user_id)
        today_str = today_tz(session.get('timezone')).isoformat()
        all_races = firestore_helper.get_all_races()
        upcoming_races = [r for r in all_races if r.get('event_date', '') >= today_str]
        result = goal_setup_chat(message, history, raw_data, user_name=user_name, coaching_rules=coaching_rules, weekly_summaries=weekly_summaries, user_tz=session.get('timezone'), user_profile=user_profile, upcoming_races=upcoming_races)

        # When Sento just produced a goal_draft with event_date + race_type,
        # look up existing races so the frontend can ask inline (not via modal).
        race_candidates = []
        goal_draft = result.get('goal_draft')
        if goal_draft and goal_draft.get('event_date') and goal_draft.get('race_type'):
            try:
                raw_candidates = firestore_helper.find_similar_races(
                    goal_draft['race_type'], goal_draft['event_date']
                )
                for r in raw_candidates:
                    participant_names = [
                        p.get('name', '') for p in r.get('participants', {}).values()
                        if p.get('name')
                    ]
                    race_candidates.append({
                        'id': r['id'],
                        'name': r.get('name', ''),
                        'race_type': r.get('race_type', ''),
                        'event_date': r.get('event_date', ''),
                        'participant_count': r.get('participant_count', 0),
                        'participant_names': participant_names,
                    })
            except Exception as _e:
                logger.warning(f"find_similar_races in chat failed: {_e}")
        result['race_candidates'] = race_candidates

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
    # Admin viewing another user: allow uid override
    requested_uid = request.args.get('uid', '').strip()
    if requested_uid and session.get('is_admin'):
        user_id = requested_uid
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
    # Users with garmin_sync_disabled and no CSV imported yet still get Sento chat
    if not raw_data:
        user_doc = firestore_helper.get_user(user_id) or {}
        if not user_doc.get('garmin_sync_disabled'):
            return jsonify({"error": "No hay datos disponibles. Recarga primero."}), 404
        from tz_utils import today_tz
        _mk = today_tz(session.get('timezone')).strftime('%Y-%m')
        raw_data = {"months": {_mk: {"activities": [], "daily_stats": {}}}, "metadata": {}, "user_profile": {}}

    try:
        from ai_advisor import ask_ai_with_context
        goal = _get_goal(user_id)
        coaching_rules = firestore_helper.get_coaching_rules()
        t_plan = _get_training_plan(user_id)
        weekly_summaries = firestore_helper.get_weekly_summaries(user_id)
        user_doc_chat = firestore_helper.get_user(user_id) or {}
        plan_schedule = user_doc_chat.get('training_plan_schedule')
        dashboard_data = process_dashboard_data(raw_data, training_goal=goal)
        user_name = session.get('display_name', 'Atleta').split()[0]
        answer = ask_ai_with_context(question, dashboard_data, raw_data, history=history, user_name=user_name, training_goal=goal, coaching_rules=coaching_rules, training_plan=t_plan, weekly_summaries=weekly_summaries, user_tz=session.get('timezone'), training_plan_schedule=plan_schedule)

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
        user_profile = firestore_helper.get_latest_assessment(user_id)
        plan = generate_training_plan_schedule(
            goal, weekly_summaries=weekly_summaries,
            user_name=user_name, user_tz=user_tz,
            user_profile=user_profile
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

    # Detect stale plan: goal was updated after the plan was generated
    plan_stale = False
    if plan and goal:
        plan_event = (plan.get('event_date') or '').strip()
        goal_event = (goal.get('event_date') or '').strip()
        plan_pace = (plan.get('goal_pace') or '').replace(' /km', '').strip()
        goal_pace = (goal.get('target_pace_str') or '').strip()
        if (plan_event and goal_event and plan_event != goal_event) or \
           (plan_pace and goal_pace and plan_pace != goal_pace):
            plan_stale = True

    is_premium = bool(user_doc.get('is_premium'))
    can_regenerate, next_regen_date = _get_regen_status(user_doc, session.get('timezone'))

    return render_template('training_plan.html',
                           plan=plan, goal=goal, user_name=user_name,
                           current_week=current_week,
                           week_dates=week_dates,
                           today_str=today.isoformat(),
                           generating=generating,
                           plan_stale=plan_stale,
                           can_regenerate=can_regenerate,
                           next_regen_date=next_regen_date,
                           is_premium=is_premium)


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

    # Limit plan generation to once per calendar month (premium users are exempt)
    user_tz = session.get('timezone')
    can_regen, next_date = _get_regen_status(user_doc, user_tz)
    if not can_regen:
        return jsonify({
            "error": f"Ya generaste tu plan este mes. Podrás regenerarlo el {next_date}.",
            "limit_reached": True,
            "next_date": next_date,
        }), 429

    weekly_summaries = firestore_helper.get_weekly_summaries(user_id)
    user_name = session.get('display_name', 'Atleta').split()[0]
    threading.Thread(
        target=_generate_plan_background,
        args=(user_id, goal, user_name, user_tz, weekly_summaries),
        daemon=True
    ).start()
    return jsonify({"status": "ok", "redirect": "/plan"})
