import json
import logging
import os
import shutil
import tempfile
import threading
import time

from flask import Blueprint, jsonify, request

from tz_utils import now_cdmx

import firestore_helper
from export_data import fetch_data_recent, init_api
from gcs_helper import GCSHelper

logger = logging.getLogger(__name__)

cron_bp = Blueprint('cron', __name__, url_prefix='/internal')

BUCKET_NAME = os.environ.get('GARMIN_BUCKET', 'garmin-dashboard-data')
DATA_FILE = 'training_data_monthly.json'


@cron_bp.route('/token-warmup')
def token_warmup():
    """Refresh oauth2 tokens in background — returns 200 immediately to avoid gunicorn timeout."""
    apikey = request.args.get('apikey', '')
    expected = os.environ.get('CRON_API_KEY')
    if expected and apikey != expected:
        return jsonify({"error": "Unauthorized"}), 401

    users = firestore_helper.get_all_active_users()
    logger.info(f"Token warmup triggered: {len(users)} users — running in background.")
    threading.Thread(target=_run_token_warmup, args=(users,), daemon=True).start()
    return jsonify({"status": "started", "users": len(users)})


def _run_token_warmup(users: list):
    for i, user in enumerate(users):
        uid = user['uid']
        if user.get('garmin_sync_disabled'):
            logger.info(f"Token warmup: skipping {uid} — Garmin sync disabled.")
            continue
        if i > 0:
            time.sleep(120)  # stagger expiry: users 1-N expire at +2min, +4min...
        tmp_dir = tempfile.mkdtemp()
        try:
            gcs = GCSHelper(BUCKET_NAME)
            token_prefix = f"users/{uid}/tokens/"
            gcs.download_directory(token_prefix, tmp_dir)
            api = init_api(token_dir=tmp_dir)
            if not api:
                raise RuntimeError("init_api returned None")
            gcs.upload_directory(tmp_dir, token_prefix)
            logger.info(f"Token warmup: refreshed token for {uid}.")
        except Exception as e:
            logger.error(f"Token warmup failed for {uid}: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info("Token warmup complete.")


@cron_bp.route('/cron-refresh')
def cron_refresh():
    """Triggers daily data refresh in background — returns 200 immediately to avoid gunicorn timeout."""
    apikey = request.args.get('apikey', '')
    expected = os.environ.get('CRON_API_KEY')
    if expected and apikey != expected:
        return jsonify({"error": "Unauthorized"}), 401

    users = firestore_helper.get_all_active_users()
    logger.info(f"Cron refresh triggered: {len(users)} users — running in background.")
    threading.Thread(target=_run_cron_refresh, args=(users,), daemon=True).start()
    return jsonify({"status": "started", "users": len(users)})


def _run_cron_refresh(users: list):
    success, failed = [], []
    for i, user in enumerate(users):
        uid = user['uid']
        if i > 0:
            time.sleep(20)
        if user.get('garmin_sync_disabled'):
            logger.info(f"Cron: skipping {uid} — Garmin sync disabled by admin.")
            success.append(uid)
            continue
        try:
            _refresh_user(user)
            success.append(uid)
        except Exception as e:
            logger.error(f"Cron: failed for {uid}: {e}")
            failed.append({"uid": uid, "error": str(e)})
    logger.info(f"Cron refresh complete: {len(success)} ok, {len(failed)} failed.")


def _refresh_user(user: dict):
    """Fetches only the last 14 days — fast and memory-efficient for daily cron."""
    uid = user['uid']
    user_name = user.get('display_name', 'Atleta').split()[0]
    tmp_dir = tempfile.mkdtemp()
    try:
        gcs = GCSHelper(BUCKET_NAME)
        token_prefix = f"users/{uid}/tokens/"
        gcs.download_directory(token_prefix, tmp_dir)

        api = init_api(token_dir=tmp_dir)
        if not api:
            # Persistent 429 — flag user to reconnect Garmin on next login
            gcs.delete_directory(token_prefix)
            firestore_helper.upsert_user(uid, {'needs_garmin_reconnect': True})
            logger.warning(f"Cron: flagged {uid} for Garmin reconnect (persistent 429).")
            raise RuntimeError(f"Failed to init Garmin API for {uid} — flagged for reconnect")

        gcs.upload_directory(tmp_dir, token_prefix)

        data_blob = f"users/{uid}/{DATA_FILE}"
        existing_str = gcs.load_json(data_blob)
        existing_data = json.loads(existing_str) if existing_str else None

        # Only fetch last 14 days — preserves full 6-month history in GCS
        # AI prescription is intentionally skipped here: it's generated on first user login
        new_data = fetch_data_recent(api, existing_data, days=14)

        gcs.save_json(data_blob, json.dumps(new_data, indent=2, ensure_ascii=False))
        firestore_helper.upsert_user(uid, {'last_refresh': now_cdmx().isoformat()})
        logger.info(f"Cron: refresh complete for {uid}.")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
