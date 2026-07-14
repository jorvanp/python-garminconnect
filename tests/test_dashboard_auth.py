"""
Tests for the dashboard login gate after the Garmin integration was removed.

login_required must now depend only on `user_id` being in the session — there is
no more `garmin_connected` flag to gate on, since Garmin is no longer connected
live and CSV import is the only data source. These tests exercise the decorator
directly with a minimal Flask app/session, with no Firestore/GCS access involved.
"""
from flask import Flask, session

from routes.dashboard import login_required


def _build_app():
    app = Flask(__name__)
    app.secret_key = "test"

    @app.route("/protected")
    @login_required
    def protected():
        return "ok"

    @app.route("/login")
    def login():
        return "login page"

    app.add_url_rule("/login", endpoint="auth.login", view_func=login)
    return app


class TestLoginRequired:
    def test_redirects_to_login_without_user_id(self):
        app = _build_app()
        client = app.test_client()
        resp = client.get("/protected")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_allows_access_with_only_user_id_in_session(self):
        """No garmin_connected flag required anymore — user_id alone is enough."""
        app = _build_app()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "abc123"
        resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.data == b"ok"
