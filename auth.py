import logging
import os
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
        'last_login': now_cdmx().isoformat(),
    }
    if not existing:
        update_data['garmin_connected'] = False
        update_data['refresh_today_count'] = 0
        update_data['last_refresh_today_date'] = ''

    firestore_helper.upsert_user(uid, update_data)

    user = firestore_helper.get_user(uid)
    session['user_id'] = uid
    session['email'] = email
    session['display_name'] = userinfo.get('name', email)
    session['picture_url'] = userinfo.get('picture', '')
    session['is_admin'] = is_admin
    session['garmin_connected'] = user.get('garmin_connected', False)
    session['timezone'] = user.get('timezone', 'America/Mexico_City')
    session['needs_login_refresh'] = True  # Always refresh on fresh login

    if not session['garmin_connected']:
        return redirect(url_for('onboarding.warning'))

    return redirect(url_for('dashboard.index'))


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
