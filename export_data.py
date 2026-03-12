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


def init_api() -> Garmin | None:
    """Initialize Garmin API using existing stored tokens."""
    tokenstore = os.getenv("GARMINTOKENS", "~/.garminconnect")
    tokenstore_path = Path(tokenstore).expanduser()

    if not tokenstore_path.exists():
        logger.error(f"No tokens found at {tokenstore_path}.")
        return None

    try:
        logger.info("Initializing API with stored tokens...")
        garmin = Garmin()
        garmin.login(str(tokenstore_path))
        return garmin
    except Exception as e:
        logger.error(f"Failed to login: {e}")
        return None


def fetch_data_monthly(api: Garmin) -> dict:
    """Fetch health and activity data for the last 6 full months + current month."""
    today = date.today()
    
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
    """Fetch health and activity data ONLY for the current month and merge it into existing data."""
    today = date.today()
    start_date = date(today.year, today.month, 1)
    
    if not existing_data:
        existing_data = {
            "metadata": {
                "period": "Current month refresh",
                "end_date": today.isoformat(),
                "start_date": start_date.isoformat()
            },
            "user_profile": {},
            "months": {}
        }
    
    # Update Metadata
    existing_data["metadata"]["end_date"] = today.isoformat()

    try:
        logger.info("Fetching user profile...")
        existing_data["user_profile"] = {
            "full_name": api.get_full_name(),
            "unit_system": api.get_unit_system()
        }
    except Exception as e:
        logger.warning(f"Could not fetch profile: {e}")

    # Generate or Ensure months structure
    current_dt = start_date
    while current_dt <= today:
        month_key = current_dt.strftime("%Y-%m")
        if month_key not in existing_data["months"]:
            existing_data["months"][month_key] = {
                "activities": [],
                "daily_stats": {}
            }
        current_dt += timedelta(days=1)
        
    # Get activities grouped by month
    logger.info(f"Fetching activities from {start_date.isoformat()} to {today.isoformat()}...")
    month_key = start_date.strftime("%Y-%m")
    try:
        activities = api.get_activities_by_date(start_date.isoformat(), today.isoformat())
        existing_data["months"][month_key]["activities"] = activities
    except Exception as e:
         logger.warning(f"Could not fetch activities for {month_key}: {e}")

    # For daily stats. 
    logger.info(f"Fetching daily stats from {start_date.isoformat()} to {today.isoformat()}...")
    
    current_dt = start_date
    while current_dt <= today:
        current_date_str = current_dt.isoformat()
        daily_record = {}

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

        existing_data["months"][month_key]["daily_stats"][current_date_str] = daily_record
        
        current_dt += timedelta(days=1)
        # Sleep slightly less for a single month to keep it fast for Cloud Run (Gunicorn timeout is ~120s max)
        time.sleep(0.1)

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
