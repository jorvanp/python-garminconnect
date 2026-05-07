import logging
import uuid as _uuid
from datetime import date
from google.cloud import firestore

from tz_utils import now_cdmx, today_cdmx

logger = logging.getLogger(__name__)

MAX_REFRESH_TODAY_DEFAULT = 10

_db = None


def get_db():
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def get_user(uid: str) -> dict | None:
    doc = get_db().collection('users').document(uid).get()
    return doc.to_dict() if doc.exists else None


def upsert_user(uid: str, data: dict):
    get_db().collection('users').document(uid).set(data, merge=True)


def get_all_users() -> list:
    docs = get_db().collection('users').stream()
    result = []
    for doc in docs:
        user = doc.to_dict()
        user['uid'] = doc.id
        result.append(user)
    return result


def get_all_active_users() -> list:
    docs = get_db().collection('users').where('garmin_connected', '==', True).stream()
    result = []
    for doc in docs:
        user = doc.to_dict()
        user['uid'] = doc.id
        result.append(user)
    return result


def get_system_config() -> dict:
    try:
        doc = get_db().collection('system').document('config').get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return {}


def get_max_refresh_today() -> int:
    return get_system_config().get('max_refresh_today', MAX_REFRESH_TODAY_DEFAULT)


def get_max_users() -> int:
    return get_system_config().get('max_users', 20)


def set_max_users(value: int):
    get_db().collection('system').document('config').set(
        {'max_users': value}, merge=True
    )


def count_users() -> int:
    try:
        docs = list(get_db().collection('users').stream())
        return len(docs)
    except Exception:
        return 0


@firestore.transactional
def _increment_refresh(transaction, user_ref, max_limit: int):
    snapshot = user_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False, 0

    data = snapshot.to_dict()
    today = today_cdmx().isoformat()
    stored_date = data.get('last_refresh_today_date', '')
    count = data.get('refresh_today_count', 0)

    if stored_date != today:
        count = 0

    if count >= max_limit:
        return False, count

    transaction.update(user_ref, {
        'last_refresh_today_date': today,
        'refresh_today_count': count + 1,
    })
    return True, count + 1


MAX_CHAT_MESSAGES = 100  # keep last 100 messages (~50 exchanges) per chat


def save_feedback(uid: str, email: str, text: str):
    get_db().collection('feedback').add({
        'uid': uid,
        'email': email,
        'text': text,
        'created_at': now_cdmx().isoformat(),
        'read': False,
    })


def get_all_feedback() -> list:
    try:
        docs = get_db().collection('feedback').order_by('created_at', direction='DESCENDING').stream()
        result = []
        for doc in docs:
            item = doc.to_dict()
            item['id'] = doc.id
            result.append(item)
        return result
    except Exception:
        return []


def mark_feedback_read(feedback_id: str):
    get_db().collection('feedback').document(feedback_id).update({'read': True})


def get_coaching_rules() -> list:
    """Returns the list of active coaching rules defined by the admin."""
    try:
        doc = get_db().collection('system').document('coaching_rules').get()
        if doc.exists:
            return doc.to_dict().get('rules', [])
    except Exception:
        pass
    return []


def save_coaching_rules(rules: list):
    get_db().collection('system').document('coaching_rules').set({'rules': rules})


def get_app_config() -> dict:
    """Returns global app config (garmin_enabled, etc.)."""
    try:
        doc = get_db().collection('system').document('app_config').get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return {'garmin_enabled': True}


def save_app_config(config: dict):
    get_db().collection('system').document('app_config').set(config)


def get_global_sections() -> dict:
    """Returns global sections_disabled config (applies to all users)."""
    try:
        doc = get_db().collection('system').document('sections_config').get()
        if doc.exists:
            return doc.to_dict().get('sections_disabled', {})
    except Exception:
        pass
    return {}


def save_global_sections(sections_disabled: dict):
    get_db().collection('system').document('sections_config').set(
        {'sections_disabled': sections_disabled}
    )


def get_chat_history(uid: str, chat_type: str) -> list:
    try:
        doc = get_db().collection('chat_history').document(uid).get()
        if doc.exists:
            return doc.to_dict().get(chat_type, [])
    except Exception:
        pass
    return []


def save_chat_history(uid: str, chat_type: str, history: list):
    trimmed = history[-MAX_CHAT_MESSAGES:]
    try:
        get_db().collection('chat_history').document(uid).set(
            {chat_type: trimmed}, merge=True
        )
    except Exception as e:
        logger.warning(f"Failed to save chat history for {uid}/{chat_type}: {e}")


def clear_chat_history(uid: str, chat_type: str):
    try:
        get_db().collection('chat_history').document(uid).set(
            {chat_type: []}, merge=True
        )
    except Exception as e:
        logger.warning(f"Failed to clear chat history for {uid}/{chat_type}: {e}")


def check_and_increment_refresh_today(uid: str) -> tuple:
    db = get_db()
    user_ref = db.collection('users').document(uid)
    max_limit = get_max_refresh_today()
    transaction = db.transaction()
    return _increment_refresh(transaction, user_ref, max_limit)


def save_weekly_summaries(uid: str, weeks: dict):
    """Stores weekly training summaries for a user."""
    try:
        get_db().collection('weekly_summaries').document(uid).set({
            'weeks': weeks,
            'updated_at': now_cdmx().isoformat(),
        })
    except Exception as e:
        logger.warning(f"Failed to save weekly summaries for {uid}: {e}")


def save_assessment(uid: str, data: dict) -> str:
    """Saves a user fitness assessment. Keyed by date so history is preserved."""
    today = today_cdmx().isoformat()
    data['created_at'] = now_cdmx().isoformat()
    get_db().collection('user_profiles').document(uid) \
        .collection('assessments').document(today).set(data)
    return today


def get_latest_assessment(uid: str) -> dict | None:
    """Returns the most recent fitness assessment for a user, or None."""
    try:
        docs = list(
            get_db().collection('user_profiles').document(uid)
            .collection('assessments')
            .order_by('created_at', direction=firestore.Query.DESCENDING)
            .limit(1).stream()
        )
        if docs:
            result = docs[0].to_dict()
            result['_date'] = docs[0].id
            return result
    except Exception as e:
        logger.warning(f"Failed to get latest assessment for {uid}: {e}")
    return None


def get_assessment_history(uid: str) -> list:
    """Returns all assessments for a user, newest first."""
    try:
        docs = get_db().collection('user_profiles').document(uid) \
            .collection('assessments') \
            .order_by('created_at', direction=firestore.Query.DESCENDING).stream()
        result = []
        for doc in docs:
            item = doc.to_dict()
            item['_date'] = doc.id
            result.append(item)
        return result
    except Exception as e:
        logger.warning(f"Failed to get assessment history for {uid}: {e}")
    return []


def get_weekly_summaries(uid: str) -> dict:
    """Returns the stored weekly training summaries for a user, or empty dict."""
    try:
        doc = get_db().collection('weekly_summaries').document(uid).get()
        if doc.exists:
            return doc.to_dict().get('weeks', {})
    except Exception as e:
        logger.warning(f"Failed to get weekly summaries for {uid}: {e}")
    return {}


# ──────────────────────────────────────────────
# Races / Competencias
# ──────────────────────────────────────────────

def find_similar_races(race_type: str, event_date: str) -> list:
    """Return races with the same event_date and overlapping race_type."""
    if not event_date:
        return []
    try:
        normalized = race_type.lower().strip()
        docs = get_db().collection('races').where('event_date', '==', event_date).stream()
        result = []
        for doc in docs:
            r = doc.to_dict()
            r['id'] = doc.id
            r_type = r.get('race_type', '').lower().strip()
            if r_type == normalized or normalized in r_type or r_type in normalized:
                r['participant_count'] = len(r.get('participants', {}))
                result.append(r)
        return result
    except Exception as e:
        logger.warning(f"find_similar_races error: {e}")
        return []


def create_race(race_type: str, event_date: str, name: str, uid: str, participant_data: dict) -> str:
    """Create a new race document with the first participant. Returns race_id."""
    race_id = _uuid.uuid4().hex[:8]
    data = {
        'name': name[:200],
        'race_type': race_type,
        'event_date': event_date,
        'participants': {uid: participant_data},
        'created_at': now_cdmx().isoformat(),
        'updated_at': now_cdmx().isoformat(),
    }
    get_db().collection('races').document(race_id).set(data)
    logger.info(f"Race created: {race_id} — {name}")
    return race_id


def add_participant_to_race(race_id: str, uid: str, participant_data: dict):
    """Add or update a participant in an existing race."""
    try:
        get_db().collection('races').document(race_id).update({
            f'participants.{uid}': participant_data,
            'updated_at': now_cdmx().isoformat(),
        })
    except Exception as e:
        logger.warning(f"add_participant_to_race error {race_id}/{uid}: {e}")


def get_all_races() -> list:
    """Return all races ordered by event_date ascending."""
    try:
        docs = get_db().collection('races').order_by('event_date').stream()
        result = []
        for doc in docs:
            r = doc.to_dict()
            r['id'] = doc.id
            r['participant_count'] = len(r.get('participants', {}))
            result.append(r)
        return result
    except Exception as e:
        logger.warning(f"get_all_races error: {e}")
        return []


def create_race_admin(race_type: str, event_date: str, name: str) -> str:
    """Create a race with no participants (admin-initiated). Returns race_id."""
    race_id = _uuid.uuid4().hex[:8]
    data = {
        'name': name[:200],
        'race_type': race_type,
        'event_date': event_date,
        'participants': {},
        'created_at': now_cdmx().isoformat(),
        'updated_at': now_cdmx().isoformat(),
        'admin_created': True,
    }
    get_db().collection('races').document(race_id).set(data)
    logger.info(f"Race created by admin: {race_id} — {name}")
    return race_id


def update_race(race_id: str, fields: dict):
    """Update editable fields (name, race_type, event_date) of a race."""
    try:
        fields['updated_at'] = now_cdmx().isoformat()
        get_db().collection('races').document(race_id).update(fields)
        logger.info(f"Race updated: {race_id} — {fields}")
    except Exception as e:
        logger.warning(f"update_race error {race_id}: {e}")


def delete_race(race_id: str):
    """Delete a race document."""
    try:
        get_db().collection('races').document(race_id).delete()
        logger.info(f"Race deleted: {race_id}")
    except Exception as e:
        logger.warning(f"delete_race error {race_id}: {e}")


def get_race(race_id: str) -> dict | None:
    """Get a single race document by ID."""
    try:
        doc = get_db().collection('races').document(race_id).get()
        if doc.exists:
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return None
    except Exception as e:
        logger.warning(f"get_race error {race_id}: {e}")
        return None
