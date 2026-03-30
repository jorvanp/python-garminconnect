import logging
import os

from dotenv import load_dotenv
load_dotenv()  # loads .env when running locally (no-op in Cloud Run)

from flask import Flask, redirect, url_for, session

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("garminconnect").setLevel(logging.WARNING)

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', os.urandom(32))

# Jinja2 filter: parse any ISO timestamp and display it in CDMX time.
# Handles both legacy UTC-naive strings (stored before tz fix) and
# modern offset-aware strings (stored with now_cdmx()).
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI

def _ts_cdmx(ts_str):
    if not ts_str:
        return ''
    try:
        d = _dt.fromisoformat(str(ts_str))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_ZI('UTC'))   # legacy: was stored as UTC
        return d.astimezone(_ZI('America/Mexico_City')).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return str(ts_str)[:16].replace('T', ' ')

app.jinja_env.filters['ts_cdmx'] = _ts_cdmx

# Google OAuth
from auth import auth_bp, init_oauth
init_oauth(app)
app.register_blueprint(auth_bp)

# Feature blueprints
from routes.dashboard import dashboard_bp
from routes.onboarding import onboarding_bp
from routes.admin import admin_bp
from routes.cron import cron_bp

app.register_blueprint(dashboard_bp)
app.register_blueprint(onboarding_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(cron_bp)


@app.route('/')
def root():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))  # shows login page with license count
    if not session.get('garmin_connected'):
        return redirect(url_for('onboarding.warning'))
    return redirect(url_for('dashboard.index'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
