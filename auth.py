import logging
import os
from datetime import datetime, timezone
from flask import Blueprint, redirect, url_for, session
from authlib.integrations.flask_client import OAuth

import firestore_helper
from tz_utils import now_cdmx

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
oauth = OAuth()

ADMIN_EMAILS = {'jorvanp@gmail.com'}


def init_oauth(app):
    app.config['GOOGLE_CLIENT_ID'] = os.environ.get('OAUTH_CLIENT_ID')
    app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('OAUTH_CLIENT_SECRET')
    oauth.init_app(app)
    oauth.register(
        name='google',
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )


@auth_bp.route('/login')
def login():
    from flask import render_template
    current_count = firestore_helper.count_users()
    max_users = firestore_helper.get_max_users()
    available = max(0, max_users - current_count)
    return render_template('login.html',
                           capacity_error=False,
                           current_count=current_count,
                           max_users=max_users,
                           available=available)


@auth_bp.route('/google')
def login_google():
    redirect_uri = url_for('auth.callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route('/callback')
def callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get('userinfo')
    if not userinfo:
        logger.error("OAuth callback: no userinfo in token.")
        return redirect(url_for('auth.login'))

    uid = userinfo['sub']
    email = userinfo.get('email', '')
    is_admin = email in ADMIN_EMAILS

    existing = firestore_helper.get_user(uid)

    if not existing and not is_admin:
        current_count = firestore_helper.count_users()
        max_users = firestore_helper.get_max_users()
        if current_count >= max_users:
            logger.warning(f"User cap reached ({current_count}/{max_users}). Rejected: {email}")
            from flask import render_template
            return render_template('login.html', capacity_error=True,
                                   current_count=current_count, max_users=max_users,
                                   available=0), 403

    update_data = {
        'email': email,
        'display_name': userinfo.get('name', email),
        'picture_url': userinfo.get('picture', ''),
        'is_admin': is_admin,
        'last_login': datetime.now(timezone.utc).isoformat(),
    }
    firestore_helper.upsert_user(uid, update_data)

    user = firestore_helper.get_user(uid)

    # Determine lang: existing preference → Google locale → fallback 'es'
    _supported = {'es', 'en'}
    stored_lang = user.get('lang') if user else None
    google_locale = userinfo.get('locale') or ''
    google_lang = google_locale.split('-')[0].lower() if google_locale else ''
    if stored_lang in _supported:
        lang = stored_lang
    elif google_lang in _supported:
        lang = google_lang
        if not stored_lang:
            firestore_helper.upsert_user(uid, {'lang': lang})
    else:
        lang = 'es'

    session.permanent = True
    session['user_id'] = uid
    session['email'] = email
    session['display_name'] = userinfo.get('name', email)
    session['picture_url'] = userinfo.get('picture', '')
    session['is_admin'] = is_admin
    session['timezone'] = user.get('timezone', 'America/Mexico_City')
    session['lang'] = lang

    return redirect(url_for('dashboard.index'))


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect('/')


@auth_bp.route('/dev-login')
def dev_login():
    """Local-only login bypass. Skips Google OAuth and logs in as an existing
    Firestore user (by email). Active ONLY when LOCAL_DEV=1, so it is inert in
    production. Set DEV_LOGIN_EMAIL to choose the user (defaults to the admin)."""
    if os.environ.get('LOCAL_DEV') != '1':
        return redirect(url_for('auth.login'))

    target_email = os.environ.get('DEV_LOGIN_EMAIL') or next(iter(ADMIN_EMAILS))
    match = next((u for u in firestore_helper.get_all_users()
                  if u.get('email', '').lower() == target_email.lower()), None)
    if not match:
        return (f"dev-login: no existe usuario con email {target_email} en Firestore. "
                f"Define DEV_LOGIN_EMAIL con un email que sí exista."), 404

    uid = match['uid']
    firestore_helper.upsert_user(uid, {'last_login': datetime.now(timezone.utc).isoformat()})

    session.permanent = True
    session['user_id'] = uid
    session['email'] = match.get('email', '')
    session['display_name'] = match.get('display_name', match.get('email', 'Atleta'))
    session['picture_url'] = match.get('picture_url', '')
    session['is_admin'] = match.get('email', '') in ADMIN_EMAILS
    session['timezone'] = match.get('timezone', 'America/Mexico_City')
    session['lang'] = match.get('lang', 'es')
    logger.warning(f"DEV LOGIN (LOCAL_DEV) as {session['email']} / {uid}")

    return redirect(url_for('dashboard.index'))
