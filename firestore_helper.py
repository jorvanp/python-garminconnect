import logging
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


def get_weekly_summaries(uid: str) -> dict:
    """Returns the stored weekly training summaries for a user, or empty dict."""
    try:
        doc = get_db().collection('weekly_summaries').document(uid).get()
        if doc.exists:
            return doc.to_dict().get('weeks', {})
    except Exception as e:
        logger.warning(f"Failed to get weekly summaries for {uid}: {e}")
    return {}
