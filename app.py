import os
import json
import logging
from datetime import date
from flask import Flask, render_template, request, jsonify
from pathlib import Path

from garminconnect import Garmin
from gcs_helper import GCSHelper

# Attempt to import our extraction function. We'll reuse the logic from export_data.py
# by wrapping it in a utility. Let's do that cleanly.
from export_data import fetch_data_current_month, init_api

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("garminconnect").setLevel(logging.WARNING)

app = Flask(__name__)

# Config
GCP_PROJECT = os.environ.get('GOOGLE_CLOUD_PROJECT', 'local')
BUCKET_NAME = os.environ.get('GARMIN_BUCKET', 'garmin-dashboard-data')
LOCAL_TOKENS = os.path.expanduser('~/.garminconnect')
DATA_FILE = 'training_data_monthly.json'

gcs = GCSHelper(BUCKET_NAME)


def get_garmin_client():
    """Initializes Garmin client. First tries to download tokens from GCS if running in cloud."""
    # If bucket is configured, sync tokens from bucket to local disk first
    if gcs.bucket:
        logger.info("Attempting to sync tokens from GCS to local instance...")
        gcs.download_directory('tokens/', LOCAL_TOKENS)

    api = init_api()
    
    # If successfully initialized and we have a bucket, upload the valid tokens back
    if api and gcs.bucket:
        logger.info("Syncing tokens back to GCS...")
        gcs.upload_directory(LOCAL_TOKENS, 'tokens/')
        
    return api


def load_gcs_data():
    """Loads JSON data from GCS if available."""
    if not gcs.bucket: return None
    try:
        data_str = gcs.load_json(DATA_FILE)
        if data_str:
            return json.loads(data_str)
    except Exception as e:
        logger.error(f"Failed to parse JSON from GCS: {e}")
    return None

def get_current_data():
    """Loads existing data from GCS or local depending on availability."""
    raw_data = load_gcs_data()
    # Fallback to local file (dev mode)
    if not raw_data and os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                raw_data = json.load(f)
        except Exception:
            pass
    return raw_data

from helpers import process_dashboard_data

@app.route('/')
def index():
    """Serves the dashboard. Uses cached data if available."""
    
    raw_data = get_current_data()
    if not raw_data:
        return "<center><h3>No dashboard data available. Please hit /refresh first.</h3></center>", 404
        
    dashboard_data = process_dashboard_data(raw_data)
    
    # We unpack the dictionary directly into Jinja kwargs
    return render_template('fitness_report.html', **dashboard_data)


@app.route('/refresh')
def refresh():
    """Forces an API call to Garmin for the *current month only*, updates data, and returns."""
    apikey = request.args.get('apikey', '')
    expected_key = os.environ.get('CRON_API_KEY')
    
    # Basic protection against unauthorized refreshes 
    if expected_key and apikey != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    logger.info("Manual refresh triggered.")
    api = get_garmin_client()
    
    if not api:
        return jsonify({"error": "Failed to connect to Garmin. Tokens might be expired."}), 500
        
    try:
        existing_data = get_current_data()
        
        # We only fetch this month and merge it over `existing_data`
        new_data = fetch_data_current_month(api, existing_data)
        
        # Generate AI Daily Recommendation before saving
        try:
            from ai_advisor import generate_daily_recommendation
            logger.info("Generating AI recommendations based on new data...")
            # We process the raw dict locally just to extract the variables for the prompt
            current_stats = process_dashboard_data(new_data) 
            if current_stats:
                ai_html = generate_daily_recommendation(current_stats)
                if ai_html:
                    if "metadata" not in new_data:
                        new_data["metadata"] = {}
                    new_data["metadata"]["ai_recommendation"] = ai_html
        except Exception as e:
            logger.error(f"Failed to generate AI recommendation during refresh: {e}")
            
        # Save locally
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2, ensure_ascii=False)
            
        # Save to Cloud Storage
        if gcs.bucket:
            logger.info("Uploading refreshed data to GCS...")
            gcs.save_json(DATA_FILE, json.dumps(new_data, indent=2))
            
        return jsonify({"status": "success", "message": "Current month data refreshed successfully."})
        
    except Exception as e:
        logger.error(f"Error during refresh: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
