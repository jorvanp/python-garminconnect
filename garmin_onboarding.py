import logging
import shutil
import tempfile
import threading
from tz_utils import now_cdmx, today_cdmx

from garminconnect import Garmin, GarminConnectAuthenticationError

logger = logging.getLogger(__name__)

# In-memory progress tracker per user_id
# { user_id: {"pct": 0..100, "msg": "...", "type": "full"|"day"} }
_fetch_progress: dict = {}

# Set of user_ids with a refresh currently running
_refreshing: set = set()


def is_refreshing(user_id: str) -> bool:
    return user_id in _refreshing


def refresh_pending(user_id: str):
    """Mark a refresh as pending (pct=1) BEFORE the thread starts, to avoid stale pct=100."""
    _fetch_progress[user_id] = {"pct": 1, "msg": "Preparando...", "type": "day"}


def refresh_start(user_id: str, msg: str = "Iniciando...", refresh_type: str = "full"):
    _refreshing.add(user_id)
    _fetch_progress[user_id] = {"pct": 2, "msg": msg, "type": refresh_type}


def refresh_progress(user_id: str, pct: int, msg: str):
    _fetch_progress[user_id] = {**_fetch_progress.get(user_id, {}), "pct": pct, "msg": msg}


def refresh_done(user_id: str, msg: str = "¡Listo!"):
    _refreshing.discard(user_id)
    _fetch_progress[user_id] = {**_fetch_progress.get(user_id, {}), "pct": 100, "msg": msg}


def refresh_error(user_id: str, msg: str):
    _refreshing.discard(user_id)
    _fetch_progress[user_id] = {**_fetch_progress.get(user_id, {}), "pct": -1, "msg": msg}


def login_garmin_and_save_tokens(email: str, password: str, user_id: str, gcs) -> tuple:
    """
    Logs into Garmin with provided credentials, uploads session tokens to GCS.

    Credentials are held only in memory for the duration of this call and are
    NEVER written to disk, Firestore, or GCS. Only the resulting session tokens
    (equivalent to browser cookies) are persisted in GCS.

    Returns:
        (success: bool, error_message: str)
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        logger.info(f"Attempting Garmin login for user {user_id}...")
        garmin = Garmin(email=email, password=password)
        # login() without tokenstore authenticates with credentials directly.
        # Passing the tmp_dir would attempt to *read* tokens (which don't exist yet).
        garmin.login()
        # Now persist the generated tokens to the temp directory.
        garmin.garth.dump(tmp_dir)

        gcs_token_prefix = f"users/{user_id}/tokens/"
        success = gcs.upload_directory(tmp_dir, gcs_token_prefix)

        if not success:
            return False, "No se pudieron guardar los tokens. Intenta de nuevo."

        logger.info(f"Garmin tokens saved to GCS for user {user_id}.")
        return True, ""

    except GarminConnectAuthenticationError:
        return False, "Credenciales de Garmin incorrectas. Verifica tu email y contraseña."
    except Exception as e:
        logger.error(f"Garmin login error for {user_id}: {e}")
        return False, f"Error al conectar con Garmin: {str(e)}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def get_fetch_progress(user_id: str) -> dict:
    return _fetch_progress.get(user_id, {"pct": 0, "msg": "Iniciando descarga..."})


def fetch_initial_data_async(user_id: str, gcs, fh_module):
    """
    Launches a background thread to perform the initial full data fetch (6 months).
    Runs independently so the HTTP response is returned immediately to the user.
    """
    if is_refreshing(user_id):
        logger.info(f"fetch_initial_data_async: refresh already running for {user_id}, skipping.")
        return

    def _run():
        tmp_dir = tempfile.mkdtemp()
        refresh_start(user_id, "Conectando con Garmin...", "full")
        try:
            from export_data import init_api
            import json
            from datetime import date, timedelta
            import time

            token_prefix = f"users/{user_id}/tokens/"
            gcs.download_directory(token_prefix, tmp_dir)

            refresh_progress(user_id, 5, "Autenticando con Garmin Connect...")
            api = init_api(token_dir=tmp_dir)
            if not api:
                refresh_error(user_id, "Error: no se pudo autenticar con Garmin.")
                logger.error(f"Background fetch: failed to init Garmin API for {user_id}.")
                return

            refresh_progress(user_id, 8, "Leyendo perfil de usuario...")
            logger.info(f"Background fetch: starting full data pull for {user_id}...")

            # ---- Inline fetch_data_monthly with progress updates ----
            today = today_cdmx()
            start_month = today.month - 6
            start_year = today.year
            if start_month <= 0:
                start_month += 12
                start_year -= 1
            start_date = date(start_year, start_month, 1)
            total_days = (today - start_date).days + 1

            export_data = {
                "metadata": {"period": "Last 6 full months + current month",
                              "end_date": today.isoformat(), "start_date": start_date.isoformat()},
                "user_profile": {},
                "months": {},
            }

            try:
                export_data["user_profile"] = {
                    "full_name": api.get_full_name(),
                    "unit_system": api.get_unit_system(),
                }
            except Exception:
                pass

            # Build months skeleton
            cur = start_date
            while cur <= today:
                mk = cur.strftime("%Y-%m")
                export_data["months"].setdefault(mk, {"activities": [], "daily_stats": {}})
                cur += timedelta(days=1)

            # Fetch activities per month
            refresh_progress(user_id, 10, "Descargando actividades...")
            cur = start_date
            while cur <= today:
                next_m = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
                chunk_end = min(next_m - timedelta(days=1), today)
                mk = cur.strftime("%Y-%m")
                try:
                    acts = api.get_activities_by_date(cur.isoformat(), chunk_end.isoformat())
                    export_data["months"][mk]["activities"] = acts
                except Exception:
                    pass
                cur = next_m
                time.sleep(0.5)

            # Fetch daily stats with progress
            refresh_progress(user_id, 20, "Descargando métricas diarias...")
            cur = start_date
            count = 0
            while cur <= today:
                ds = cur.isoformat()
                mk = cur.strftime("%Y-%m")
                rec = {}
                try:
                    rec["summary"] = api.get_stats(ds)
                except Exception:
                    pass
                try:
                    rec["sleep"] = api.get_sleep_data(ds)
                except Exception:
                    pass
                try:
                    rhr_val = api.get_rhr_day(ds)
                    if rhr_val:
                        rec["resting_heart_rate"] = rhr_val
                except Exception:
                    pass
                try:
                    stress_val = api.get_stress_data(ds)
                    if stress_val:
                        rec["stress"] = stress_val
                except Exception:
                    pass
                export_data["months"][mk]["daily_stats"][ds] = rec
                count += 1
                pct = 20 + int((count / total_days) * 75)
                refresh_progress(user_id, min(pct, 94), f"Día {count}/{total_days} — {ds}")
                cur += timedelta(days=1)
                time.sleep(0.3)
            # ---- End inline fetch ----

            refresh_progress(user_id, 96, "Guardando datos en la nube...")
            data_blob = f"users/{user_id}/training_data_monthly.json"
            gcs.save_json(data_blob, json.dumps(export_data, indent=2, ensure_ascii=False))

            try:
                from weekly_summarizer import compute_weekly_summaries
                summaries = compute_weekly_summaries(export_data)
                fh_module.save_weekly_summaries(user_id, summaries)
            except Exception as e:
                logger.error(f"Weekly summary error during full fetch for {user_id}: {e}")

            fh_module.upsert_user(user_id, {'last_refresh': now_cdmx().isoformat()})
            refresh_done(user_id, "¡Listo! Redirigiendo al dashboard...")
            logger.info(f"Background fetch: complete for {user_id}.")
        except Exception as e:
            refresh_error(user_id, f"Error: {str(e)}")
            logger.error(f"Background fetch error for {user_id}: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
