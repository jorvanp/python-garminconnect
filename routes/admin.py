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

    from tz_utils import today_cdmx

    gcs = GCSHelper(BUCKET_NAME)
    data_blob = f"users/{uid}/{DATA_FILE}"
    raw_str = gcs.load_json(data_blob)
    training_goal = user.get('training_goal')
    garmin_sync_disabled = user.get('garmin_sync_disabled', False)

    raw_data = json.loads(raw_str) if raw_str else None
    dashboard_data = process_dashboard_data(raw_data, training_goal=training_goal)

    # For users without Garmin (CSV mode), build an empty skeleton instead of error page
    if not dashboard_data:
        if garmin_sync_disabled or not user.get('garmin_connected'):
            _mk = today_cdmx().strftime('%Y-%m')
            _empty = {'months': {_mk: {'activities': [], 'daily_stats': {}}}, 'metadata': {}, 'user_profile': {}}
            dashboard_data = process_dashboard_data(_empty, training_goal=training_goal)
        else:
            email = user.get('email', uid)
            name = user.get('display_name', email)
            return render_template('admin_no_data.html', email=email, name=name, uid=uid), 202

    dashboard_data['_admin_viewing'] = True
    dashboard_data['_admin_user_email'] = user.get('email', uid)
    dashboard_data['_viewed_uid'] = uid
    dashboard_data['refresh_count'] = 0
    dashboard_data['max_refresh'] = 0
    dashboard_data['last_refresh'] = user.get('last_refresh', '')
    dashboard_data['garmin_sync_disabled'] = garmin_sync_disabled

    from routes.dashboard import _add_sections, _add_race_hero
    _add_race_hero(dashboard_data, user, None)
    _add_sections(dashboard_data)

    all_races = sorted(firestore_helper.get_all_races(),
                       key=lambda r: r.get('event_date', ''))
    dashboard_data['_all_races'] = all_races
    dashboard_data['_current_race_id'] = (user.get('training_goal') or {}).get('race_id', '')

    return render_template('fitness_report.html', **dashboard_data)


@admin_bp.route('/user/<uid>/profile')
@admin_required
def view_user_profile(uid: str):
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    history = firestore_helper.get_assessment_history(uid)
    return render_template('admin_user_profile.html',
                           user=user, history=history,
                           email=user.get('email', uid))


@admin_bp.route('/user/<uid>/plan')
@admin_required
def view_user_plan(uid: str):
    from datetime import date as _date, timedelta as _td
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    plan = user.get('training_plan_schedule')
    if not plan:
        return f"<center><h3>No hay plan generado para {user.get('email', uid)}</h3><a href='/admin/'>← Admin</a></center>", 404
    goal = user.get('training_goal')
    user_name = (user.get('display_name') or user.get('email', 'Atleta')).split()[0]
    from tz_utils import today_tz
    today = today_tz(None)
    _MONTHS_ES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']
    _DAYS_ES   = ['lun','mar','mié','jue','vie','sáb','dom']
    def _fmt(d): return f"{_DAYS_ES[d.weekday()]} {d.day} {_MONTHS_ES[d.month-1]}"
    current_week = None
    week_dates = {}
    plan_start_str = plan.get('plan_start_date', '')
    if plan_start_str:
        try:
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
                           current_week=current_week, week_dates=week_dates,
                           today_str=today.isoformat(), generating=False,
                           _admin_viewing=True,
                           _admin_user_email=user.get('email', uid),
                           _viewed_uid=uid)


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
    """Full historical refresh: re-fetches last 2 months (async background thread)."""
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


@admin_bp.route('/user/<uid>/toggle-premium', methods=['POST'])
@admin_required
def toggle_premium(uid: str):
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    new_val = not bool(user.get('is_premium'))
    firestore_helper.upsert_user(uid, {'is_premium': new_val})
    logger.info(f"Admin set is_premium={new_val} for {uid}")
    return jsonify({"status": "ok", "is_premium": new_val})


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


SECTIONS = {
    'snapshot':        'Snapshot de Hoy',
    'chat':            'Sento IA — Pregúntale a tus datos',
    'activities':      'Actividades últimos 7 días',
    'weekly_progress': 'Progreso Semanal',
}


@admin_bp.route('/app-config', methods=['GET'])
@admin_required
def get_app_config():
    config = firestore_helper.get_app_config()
    config['max_users'] = firestore_helper.get_max_users()
    config['current_users'] = firestore_helper.count_users()
    return jsonify(config)


@admin_bp.route('/set-max-users', methods=['POST'])
@admin_required
def set_max_users():
    data = request.get_json(silent=True) or {}
    try:
        value = int(data.get('max_users', 0))
        if value < 1 or value > 500:
            return jsonify({"error": "Valor debe estar entre 1 y 500"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Valor inválido"}), 400
    firestore_helper.set_max_users(value)
    logger.info(f"Admin updated max_users to {value}")
    return jsonify({"status": "ok", "max_users": value})


@admin_bp.route('/app-config/toggle-garmin', methods=['POST'])
@admin_required
def toggle_garmin():
    config = firestore_helper.get_app_config()
    new_val = not config.get('garmin_enabled', True)
    config['garmin_enabled'] = new_val
    firestore_helper.save_app_config(config)
    logger.info(f"Admin {'enabled' if new_val else 'disabled'} Garmin globally")
    return jsonify({"status": "ok", "garmin_enabled": new_val})


@admin_bp.route('/sections', methods=['GET'])
@admin_required
def get_sections():
    return jsonify(firestore_helper.get_global_sections())


@admin_bp.route('/sections/toggle', methods=['POST'])
@admin_required
def toggle_section():
    data = request.get_json(silent=True) or {}
    section = data.get('section')
    if section not in SECTIONS:
        return jsonify({"error": "Sección inválida"}), 400
    current = firestore_helper.get_global_sections()
    new_val = not current.get(section, False)
    current[section] = new_val
    firestore_helper.save_global_sections(current)
    logger.info(f"Admin globally {'disabled' if new_val else 'enabled'} section '{section}'")
    return jsonify({"status": "ok", "section": section, "disabled": new_val})


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


@admin_bp.route('/races')
@admin_required
def races_view():
    races = firestore_helper.get_all_races()
    all_users = firestore_helper.get_all_users()
    users = {u['uid']: u for u in all_users}

    # Build race_id → [user_info] for users assigned via admin (training_goal.race_id)
    admin_assigned: dict = {}
    for u in all_users:
        goal = u.get('training_goal') or {}
        rid = goal.get('race_id')
        if rid:
            admin_assigned.setdefault(rid, []).append({
                'uid': u.get('uid', ''),
                'name': u.get('display_name') or u.get('email', '—'),
                'email': u.get('email', ''),
                'pace_target': goal.get('target_pace_str', ''),
                'weekly_peak_km': goal.get('weekly_peak_km', ''),
            })

    return render_template('admin_races.html', races=races, users=users,
                           admin_assigned=admin_assigned)


@admin_bp.route('/races', methods=['POST'])
@admin_required
def create_race_admin():
    from datetime import date as _date
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    race_type = (data.get('race_type') or '').strip()
    event_date = (data.get('event_date') or '').strip()
    if not name or not race_type or not event_date:
        return jsonify({"error": "Nombre, tipo y fecha son requeridos."}), 400
    try:
        _date.fromisoformat(event_date)
    except ValueError:
        return jsonify({"error": "Fecha inválida."}), 400
    race_id = firestore_helper.create_race_admin(race_type, event_date, name)
    logger.info(f"Admin created race {race_id}: {name}")
    return jsonify({"status": "ok", "race_id": race_id})


@admin_bp.route('/races/<race_id>', methods=['PATCH'])
@admin_required
def update_race(race_id: str):
    from datetime import date as _date
    data = request.get_json(silent=True) or {}
    allowed = {}
    if 'name' in data:
        name = (data['name'] or '').strip()[:200]
        if not name:
            return jsonify({"error": "El nombre no puede estar vacío."}), 400
        allowed['name'] = name
    if 'race_type' in data:
        race_type = (data['race_type'] or '').strip()
        if not race_type:
            return jsonify({"error": "El tipo no puede estar vacío."}), 400
        allowed['race_type'] = race_type
    if 'event_date' in data:
        event_date = (data['event_date'] or '').strip()
        try:
            _date.fromisoformat(event_date)
        except ValueError:
            return jsonify({"error": "Fecha inválida."}), 400
        allowed['event_date'] = event_date
    if not allowed:
        return jsonify({"error": "Nada que actualizar."}), 400
    firestore_helper.update_race(race_id, allowed)
    return jsonify({"status": "ok"})


@admin_bp.route('/races/<race_id>', methods=['DELETE'])
@admin_required
def delete_race(race_id: str):
    firestore_helper.delete_race(race_id)
    return jsonify({"status": "ok"})


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # 20 MB — límite de recepción
_HERO_MAX_WIDTH   = 1400               # px máximos para la imagen de portada
_HERO_JPEG_Q      = 82                 # calidad JPEG de salida


def _resize_image(data: bytes, max_width: int = _HERO_MAX_WIDTH, quality: int = _HERO_JPEG_Q) -> bytes:
    """Redimensiona y recomprime la imagen al ancho máximo indicado en JPEG."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(data))
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return buf.getvalue()


@admin_bp.route('/races/<race_id>/image', methods=['POST'])
@admin_required
def upload_race_image(race_id: str):
    if 'image' not in request.files:
        return jsonify({"error": "No se recibió archivo de imagen."}), 400
    img_file = request.files['image']
    if not img_file or not img_file.filename:
        return jsonify({"error": "Archivo vacío."}), 400
    if not (img_file.content_type or '').startswith('image/'):
        return jsonify({"error": "Solo se permiten imágenes."}), 400
    data = img_file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return jsonify({"error": f"Imagen demasiado grande (máx {_MAX_UPLOAD_BYTES // 1024 // 1024} MB)."}), 400
    try:
        data = _resize_image(data)
    except Exception as e:
        logger.warning(f"Image resize failed for race {race_id}: {e}")
        return jsonify({"error": "No se pudo procesar la imagen. Verifica que sea un archivo de imagen válido."}), 400
    gcs = GCSHelper(BUCKET_NAME)
    ok = gcs.upload_bytes(f"race_images/{race_id}", data, 'image/jpeg')
    if not ok:
        return jsonify({"error": "Error al guardar la imagen."}), 500
    image_url = f"/races/{race_id}/image"
    firestore_helper.update_race(race_id, {'image_url': image_url, 'image_content_type': 'image/jpeg'})
    logger.info(f"Admin uploaded image for race {race_id} ({len(data)//1024} KB after resize)")
    return jsonify({"status": "ok", "image_url": image_url})


@admin_bp.route('/user/<uid>/assign-race', methods=['POST'])
@admin_required
def assign_race(uid: str):
    """Associate an existing race to a user's training_goal.race_id."""
    user = firestore_helper.get_user(uid)
    if not user:
        abort(404)
    data = request.get_json(silent=True) or {}
    race_id = (data.get('race_id') or '').strip()
    goal = dict(user.get('training_goal') or {})
    if not race_id:
        goal.pop('race_id', None)
        firestore_helper.upsert_user(uid, {'training_goal': goal})
        logger.info(f"Admin unlinked race from user {uid}")
        return jsonify({"status": "ok", "race_id": None})
    race = firestore_helper.get_race(race_id)
    if not race:
        return jsonify({"error": "Competencia no encontrada."}), 404
    goal['race_id'] = race_id
    firestore_helper.upsert_user(uid, {'training_goal': goal})
    logger.info(f"Admin assigned race {race_id} to user {uid}")
    return jsonify({"status": "ok", "race_id": race_id,
                    "race_name": race.get('name'), "race_date": race.get('event_date')})


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
