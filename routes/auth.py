"""Authentication routes for the single-admin session flow."""

import yaml
from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from app import admin_setup, security
from app.credentials import verify_credential
from app.logging_utils import safe_exception_details

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/setup', methods=['GET', 'POST'])
def setup():
    import webui

    if not admin_setup.is_pending(webui.config):
        return 'Administrator setup is unavailable.', 404
    if request.method == 'GET':
        return render_template('setup.html')
    request.max_content_length = 8192
    try:
        csrf = webui.get_request_csrf_token()
        if (not isinstance(csrf, str) or len(csrf) > 128 or not csrf.isascii()
                or not webui.is_valid_csrf_token(csrf)):
            return jsonify(error='Refresh the setup page and try again.'), 400
        result = admin_setup.complete_setup(
            request.form.get('token', ''), request.form.get('password', ''),
            request.form.get('confirmation', ''),
        )
    except RequestEntityTooLarge:
        return jsonify(error='Setup request is too large.'), 413
    except (OSError, ValueError, yaml.YAMLError) as error:
        webui.logger.warning('Administrator setup could not be saved: %s', safe_exception_details(error))
        return jsonify(error='Setup could not be saved. Check writable configuration directory mounts.'), 503
    if result == 'success':
        session.clear()
        try:
            webui.load_config()
            webui.start_runtime_services()
        except (OSError, ValueError, yaml.YAMLError, RuntimeError) as error:
            webui.logger.warning('Administrator setup saved; runtime activation failed: %s', safe_exception_details(error))
            return jsonify(error='Password saved. Restart WAMF to reload its configuration.'), 503
        # The client follows only this fixed local destination, never user input.
        return redirect('/login', code=303)
    if result == 'password':
        return jsonify(error='Passwords must match and contain 8 to 1024 characters.'), 400
    if result == 'limited':
        response = jsonify(error='Too many setup attempts. Try again in one minute.')
        response.headers['Retry-After'] = '60'
        return response, 429
    return jsonify(error='Setup link is invalid, expired or already used.'), 400


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    import webui

    if not webui.is_admin_auth_enabled():
        return redirect(webui.get_safe_next_url())

    if session.get('admin_authenticated'):
        return redirect(webui.get_safe_next_url())

    config_error = webui.get_admin_auth_config_error()

    if config_error:
        return render_template(
            'login.html',
            error=config_error,
            next_url=webui.get_safe_next_url()
        ), 500

    error = None

    if request.method == 'POST':
        client_ip = webui.get_client_ip()

        reservation = security.reserve_login_attempt(client_ip)
        if reservation is None:
            webui.delay_login_failure()
            error = 'Login temporarily locked. Try again later.'
        else:
            authenticated = False
            try:
                password = request.form.get('password', '')
                authenticated = verify_credential(webui.get_admin_password_hash(), password)
            finally:
                security.settle_login_attempt(reservation, success=authenticated)

            if authenticated:
                session.clear()
                session['admin_authenticated'] = True
                webui.get_csrf_token()
                return redirect(webui.get_safe_next_url())

            webui.delay_login_failure()
            error = 'Login failed.'

    return render_template(
        'login.html',
        error=error,
        next_url=webui.get_safe_next_url()
    )


@auth_bp.route('/logout')
def logout():
    import webui

    if webui.app.secret_key:
        session.clear()
        flash('You have been logged out.')

    return redirect(url_for('auth.login'))
