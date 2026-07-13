import logging
import os

from dotenv import load_dotenv
load_dotenv()  # loads .env when running locally (no-op in Cloud Run)

from flask import Flask, redirect, render_template, url_for, session

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("garminconnect").setLevel(logging.WARNING)

from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', os.urandom(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
# En desarrollo local (LOCAL_DEV=1) servimos por http://localhost, donde una
# cookie Secure no se guarda y rompería el flujo OAuth (state/CSRF). En producción
# LOCAL_DEV no está seteado, así que la cookie sigue siendo Secure como siempre.
_IS_LOCAL = os.environ.get('LOCAL_DEV') == '1'
app.config['SESSION_COOKIE_SECURE'] = not _IS_LOCAL
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Cloud Run (y cualquier proxy inverso) termina TLS y reenvía via HTTP interno.
# ProxyFix hace que Flask lea X-Forwarded-Proto y genere URLs con https://
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Redirigir HTTP → HTTPS en producción (Cloud Run termina TLS y envía X-Forwarded-Proto)
from flask import request as _request

# i18n — Flask-Babel
from flask_babel import Babel, get_locale

_SUPPORTED_LOCALES = {'es', 'en'}

def _get_locale():
    lang = session.get('lang')
    if lang in _SUPPORTED_LOCALES:
        return lang
    best = _request.accept_languages.best_match(list(_SUPPORTED_LOCALES))
    return best or 'en'

babel = Babel(app, locale_selector=_get_locale)
app.config['BABEL_DEFAULT_LOCALE'] = 'en'
app.config['BABEL_TRANSLATION_DIRECTORIES'] = 'translations'
app.jinja_env.globals['get_locale'] = get_locale

@app.before_request
def _force_https():
    if _request.headers.get('X-Forwarded-Proto', 'https') == 'http':
        return redirect(_request.url.replace('http://', 'https://', 1), code=301)

@app.after_request
def _add_hsts(response):
    # Tell browsers to always use HTTPS for this domain (1 year)
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

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

import firestore_helper

# Google OAuth
from auth import auth_bp, init_oauth
init_oauth(app)
app.register_blueprint(auth_bp)

# Feature blueprints
from routes.dashboard import dashboard_bp
from routes.onboarding import onboarding_bp
from routes.admin import admin_bp
from routes.cron import cron_bp
from routes.user import user_bp

app.register_blueprint(dashboard_bp)
app.register_blueprint(onboarding_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(cron_bp)
app.register_blueprint(user_bp)



if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
