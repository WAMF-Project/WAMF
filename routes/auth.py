"""Authentication routes for the single-admin session flow."""

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash


auth_bp = Blueprint('auth', __name__)


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

        if webui.is_login_rate_limited(client_ip):
            webui.delay_login_failure()
            error = 'Login temporarily locked. Try again later.'
        else:
            password = request.form.get('password', '')

            if check_password_hash(webui.get_admin_password_hash(), password):
                session.clear()
                session['admin_authenticated'] = True
                webui.get_csrf_token()
                webui.clear_login_failures(client_ip)
                return redirect(webui.get_safe_next_url())

            webui.record_login_failure(client_ip)
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
