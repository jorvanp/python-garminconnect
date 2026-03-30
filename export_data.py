#!/usr/bin/env python3
"""
Garmin Data Exporter for AI Analysis (6 Months + Current Month)

This script retrieves garmin connect data for the last 6 full months + current month
and outputs it into a structured JSON file suitable for AI analysis.
"""
import json
import logging
import os
import time
from datetime import date, timedelta

from tz_utils import today_cdmx
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Suppress debug output from garmin library
logging.getLogger("garminconnect").setLevel(logging.WARNING)


def init_api(token_dir: str = None) -> Garmin | None:
    """Initialize Garmin API using existing stored tokens.

    Args:
        token_dir: Path to directory containing Garmin session tokens.
                   Defaults to GARMINTOKENS env var or ~/.garminconnect.
    """
    if token_dir:
        tokenstore_path = Path(token_dir)
    else:
        tokenstore = os.getenv("GARMINTOKENS", "~/.garminconnect")
        tokenstore_path = Path(tokenstore).expanduser()

    if not tokenstore_path.exists():
        logger.error(f"No tokens found at {tokenstore_path}.")
        return None

    # If the stored oauth2_token is still valid, login() won't need to call sso.exchange()
    # — this avoids hitting the 429 rate limit on the exchange endpoint entirely.
    oauth2_path = tokenstore_path / 'oauth2_token.json'
    if oauth2_path.exists():
        try:
            token_data = json.loads(oauth2_path.read_text())
            expires_at = token_data.get('expires_at', 0)
            if expires_at and expires_at > time.time() + 120:  # 2-min buffer
                logger.info("oauth2_token still valid, skipping exchange.")
            else:
                logger.info("oauth2_token expired or expiring soon — exchange will be needed.")
        except Exception:
            pass

    retry_delays = [15, 30, 60]  # seconds between retries on 429
    for attempt, delay in enumerate([0] + retry_delays):
        if delay:
            logger.warning(f"Garmin 429 rate limit — retrying in {delay}s (attempt {attempt}/{len(retry_delays)})...")
            time.sleep(delay)
        try:
            logger.info("Initializing API with stored tokens...")
            garmin = Garmin()
            garmin.login(str(tokenstore_path))
            return garmin
        except GarminConnectTooManyRequestsError as e:
            logger.warning(f"Rate limited (attempt {attempt + 1}): {e}")
        except Exception as e:
            # Also catch raw 429 HTTPError from garth's sso.exchange
            if "429" in str(e):
                logger.warning(f"Rate limited via HTTP 429 (attempt {attempt + 1}): {e}")
            else:
                logger.error(f"Failed to login: {e}")
                return None

    logger.error("Garmin API init failed after all retries (persistent 429).")
    return None


def fetch_data_monthly(api: Garmin) -> dict:
    """Fetch health and activity data for the last 6 full months + current month."""
    today = today_cdmx()
    
    current_year = today.year
    current_month = today.month
    
    # Calculate 6 full months ago
    start_month = current_month - 6
    start_year = current_year
    if start_month <= 0:
        start_month += 12
        start_year -= 1
        
    start_date = date(start_year, start_month, 1)
    
    export_data = {
        "metadata": {
            "period": "Last 6 full months + current month",
            "end_date": today.isoformat(),
            "start_date": start_date.isoformat()
        },
        "user_profile": {},
        "months": {}
    }

    try:
        logger.info("Fetching user profile...")
        export_data["user_profile"] = {
            "full_name": api.get_full_name(),
            "unit_system": api.get_unit_system()
        }
    except Exception as e:
        logger.warning(f"Could not fetch profile: {e}")

    # Generate months structure
    current_dt = start_date
    while current_dt <= today:
        month_key = current_dt.strftime("%Y-%m")
        if month_key not in export_data["months"]:
            export_data["months"][month_key] = {
                "activities": [],
                "daily_stats": {}
            }
        current_dt += timedelta(days=1)
        
    # Get activities grouped by month
    logger.info(f"Fetching activities from {start_date.isoformat()} to {today.isoformat()}...")
    try:
        current_st = start_date
        while current_st <= today:
            next_month = current_st.month + 1
            next_year = current_st.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            next_month_start = date(next_year, next_month, 1)
            chunk_end = next_month_start - timedelta(days=1)
            if chunk_end > today:
                chunk_end = today
                
            month_key = current_st.strftime("%Y-%m")
            logger.info(f"Fetching activities for {month_key} ({current_st} to {chunk_end})...")
            try:
                activities = api.get_activities_by_date(current_st.isoformat(), chunk_end.isoformat())
                export_data["months"][month_key]["activities"] = activities
            except Exception as e:
                 logger.warning(f"Could not fetch activities for {month_key}: {e}")
            
            current_st = next_month_start
            time.sleep(1) # Be nice to the API
    except Exception as e:
        logger.warning(f"Could not fetch some activities: {e}")

    # For daily stats, 6 months is ~190 days. 
    logger.info(f"Fetching daily stats from {start_date.isoformat()} to {today.isoformat()}...")
    logger.info("This will take a few minutes. Please wait...")
    
    current_dt = start_date
    count = 0
    total_days = (today - start_date).days + 1
    
    while current_dt <= today:
        current_date_str = current_dt.isoformat()
        month_key = current_dt.strftime("%Y-%m")
        daily_record = {}
        
        count += 1
        if count % 10 == 0:
            logger.info(f"Progress: [{current_date_str}] fetched {count}/{total_days} days...")

        # Basic Stats (Steps, Calories)
        try:
            daily_record["summary"] = api.get_stats(current_date_str)
        except Exception as e:
            logger.debug(f"Stats error for {current_date_str}: {e}")

        # Sleep Data
        try:
            daily_record["sleep"] = api.get_sleep_data(current_date_str)
        except Exception as e:
            logger.debug(f"Sleep error for {current_date_str}: {e}")

        # HRV Data
        try:
            rhr = api.get_rhr_day(current_date_str)
            if rhr:
                 daily_record["resting_heart_rate"] = rhr
        except Exception:
            pass
            
        # Stress Data
        try:
            stress = api.get_stress_data(current_date_str)
            if stress:
                daily_record["stress"] = stress
        except Exception:
            pass

        export_data["months"][month_key]["daily_stats"][current_date_str] = daily_record
        
        current_dt += timedelta(days=1)
        # Sleep for a fraction to avoid 429 Too Many Requests
        time.sleep(0.3)

    return export_data


def fetch_data_current_month(api: Garmin, existing_data: dict) -> dict:
    """
    Incremental refresh covering the last 6 months + today.
    - Days already stored and older than yesterday are skipped (fast).
    - Yesterday and today are always re-fetched (Garmin finalises data with ~24h lag).
    - Missing days in any month within the 6-month window are backfilled.
    """
    today = today_cdmx()
    yesterday = today - timedelta(days=1)

    # Start date: 6 months back (same day-1 logic as onboarding)
    start_month = today.month - 6
    start_year = today.year
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    start_date = date(start_year, start_month, 1)

    if not existing_data:
        existing_data = {
            "metadata": {
                "period": "Last 6 months + today (incremental)",
                "end_date": today.isoformat(),
                "start_date": start_date.isoformat(),
            },
            "user_profile": {},
            "months": {},
        }

    existing_data["metadata"]["end_date"] = today.isoformat()
    existing_data["metadata"]["start_date"] = start_date.isoformat()

    try:
        existing_data["user_profile"] = {
            "full_name": api.get_full_name(),
            "unit_system": api.get_unit_system(),
        }
    except Exception as e:
        logger.warning(f"Could not fetch profile: {e}")

    # Ensure month buckets exist for the full 6-month window
    cur = start_date
    while cur <= today:
        mk = cur.strftime("%Y-%m")
        existing_data["months"].setdefault(mk, {"activities": [], "daily_stats": {}})
        cur += timedelta(days=1)

    # Fetch activities month by month (only months missing or current month)
    logger.info(f"Fetching activities {start_date} → {today}...")
    cur = start_date
    while cur <= today:
        mk = cur.strftime("%Y-%m")
        next_m_month = cur.month % 12 + 1
        next_m_year = cur.year + (cur.month // 12)
        next_m = date(next_m_year, next_m_month, 1)
        chunk_end = min(next_m - timedelta(days=1), today)

        # Re-fetch current month always; skip older months if activities already loaded
        existing_acts = existing_data["months"][mk].get("activities", [])
        if not existing_acts or mk == today.strftime("%Y-%m"):
            try:
                acts = api.get_activities_by_date(cur.isoformat(), chunk_end.isoformat())
                existing_data["months"][mk]["activities"] = acts
            except Exception as e:
                logger.warning(f"Could not fetch activities for {mk}: {e}")

        cur = next_m

    # Fetch daily stats incrementally across the full 6-month window
    logger.info(f"Fetching daily stats {start_date} → {today} (incremental)...")
    cur = start_date
    while cur <= today:
        current_date_str = cur.isoformat()
        mk = cur.strftime("%Y-%m")

        existing_day = existing_data["months"][mk].get("daily_stats", {}).get(current_date_str)
        if existing_day and cur < yesterday:
            cur += timedelta(days=1)
            continue

        daily_record = {}
        try:
            daily_record["summary"] = api.get_stats(current_date_str)
        except Exception as e:
            logger.debug(f"Stats error for {current_date_str}: {e}")
        try:
            daily_record["sleep"] = api.get_sleep_data(current_date_str)
        except Exception as e:
            logger.debug(f"Sleep error for {current_date_str}: {e}")
        try:
            rhr = api.get_rhr_day(current_date_str)
            if rhr:
                daily_record["resting_heart_rate"] = rhr
        except Exception:
            pass
        try:
            stress = api.get_stress_data(current_date_str)
            if stress:
                daily_record["stress"] = stress
        except Exception:
            pass

        existing_data["months"][mk]["daily_stats"][current_date_str] = daily_record
        cur += timedelta(days=1)
        time.sleep(0.1)

    return existing_data


def fetch_data_recent(api: Garmin, existing_data: dict, days: int = 14) -> dict:
    """
    Lightweight refresh: only fetches the last `days` days of activities and daily stats.
    Used by the daily cron to avoid loading 6 months into memory.
    Merges results into existing_data (read from GCS), preserving older history.
    """
    today = today_cdmx()
    yesterday = today - timedelta(days=1)
    start_date = today - timedelta(days=days - 1)

    if not existing_data:
        existing_data = {
            "metadata": {"period": f"Last {days} days (incremental)", "end_date": today.isoformat()},
            "user_profile": {},
            "months": {},
        }

    existing_data["metadata"]["end_date"] = today.isoformat()

    try:
        existing_data["user_profile"] = {
            "full_name": api.get_full_name(),
            "unit_system": api.get_unit_system(),
        }
    except Exception as e:
        logger.warning(f"Could not fetch profile: {e}")

    # Ensure month buckets exist for the window
    cur = start_date
    while cur <= today:
        mk = cur.strftime("%Y-%m")
        existing_data["months"].setdefault(mk, {"activities": [], "daily_stats": {}})
        cur += timedelta(days=1)

    # Re-fetch activities for months touched by the window
    months_in_window = set()
    cur = start_date
    while cur <= today:
        months_in_window.add(cur.strftime("%Y-%m"))
        cur += timedelta(days=1)

    for mk in sorted(months_in_window):
        year, month = int(mk[:4]), int(mk[5:])
        next_m = date(year + (month // 12), month % 12 + 1, 1)
        chunk_start = max(start_date, date(year, month, 1))
        chunk_end = min(next_m - timedelta(days=1), today)
        try:
            acts = api.get_activities_by_date(chunk_start.isoformat(), chunk_end.isoformat())
            # Merge: keep activities outside the window, replace those inside
            existing_acts = existing_data["months"][mk].get("activities", [])
            outside = [a for a in existing_acts
                       if a.get("startTimeLocal", "")[:10] < chunk_start.isoformat()]
            existing_data["months"][mk]["activities"] = outside + (acts or [])
        except Exception as e:
            logger.warning(f"Could not fetch activities for {mk}: {e}")

    # Fetch daily stats only for the recent window (always yesterday + today)
    cur = start_date
    while cur <= today:
        current_date_str = cur.isoformat()
        mk = cur.strftime("%Y-%m")
        existing_day = existing_data["months"][mk].get("daily_stats", {}).get(current_date_str)
        if existing_day and cur < yesterday:
            cur += timedelta(days=1)
            continue
        daily_record = {}
        try:
            daily_record["summary"] = api.get_stats(current_date_str)
        except Exception as e:
            logger.debug(f"Stats error for {current_date_str}: {e}")
        try:
            daily_record["sleep"] = api.get_sleep_data(current_date_str)
        except Exception as e:
            logger.debug(f"Sleep error for {current_date_str}: {e}")
        try:
            rhr = api.get_rhr_day(current_date_str)
            if rhr:
                daily_record["resting_heart_rate"] = rhr
        except Exception:
            pass
        try:
            stress = api.get_stress_data(current_date_str)
            if stress:
                daily_record["stress"] = stress
        except Exception:
            pass
        existing_data["months"][mk]["daily_stats"][current_date_str] = daily_record
        cur += timedelta(days=1)
        time.sleep(0.05)

    return existing_data


def main():
    logger.info("Starting data extraction...")
    api = init_api()
    if not api:
        return

    # Extract data for the last 6 full months + March
    data_to_export = fetch_data_monthly(api)

    # Save to JSON
    output_filename = "training_data_monthly.json"
    logger.info(f"Saving data to {output_filename}...")
    
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(data_to_export, f, indent=2, ensure_ascii=False)
        
    logger.info("Data export complete!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Extraction interrupted by user.")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
