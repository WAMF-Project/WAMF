"""First-run setup state. No token generation occurs on import or HTTP requests."""

import hashlib
import hmac
import ipaddress
import math
import os
import re
import time
from urllib.parse import urlsplit

import yaml
from werkzeug.security import generate_password_hash

from app.config_editor import get_config_path
from app.config_store import atomic_write_config, config_lock

LIFETIME = 900
ATTEMPT_LIMIT = 10
ATTEMPT_WINDOW = 60
_deadlines = {}


class _SetupExpired(Exception):
    pass


def is_pending(config):
    if not isinstance(config, dict):
        return False
    admin = (config or {}).get('admin', {})
    if not isinstance(admin, dict):
        return False
    password = admin.get('password_hash')
    password_missing = password is None or isinstance(password, str) and password.strip() in (
        '', '<generated werkzeug password hash>',
    )
    return bool(
        admin.get('auth_enabled') is True
        and password_missing
        and admin.get('setup_pending') is True
    )


def valid_state(admin):
    state = admin.get('setup')
    return bool(
        admin.get('setup_pending') is True
        and isinstance(state, dict)
        and isinstance(state.get('digest'), str)
        and re.fullmatch(r'[0-9a-f]{64}', state['digest'])
        and all(type(state.get(k)) in (int, float) and math.isfinite(state[k])
                for k in ('issued_at', 'expires_at', 'last_seen', 'window_started'))
        and state['expires_at'] - state['issued_at'] == LIFETIME
        and state['issued_at'] <= state['last_seen']
        and type(state.get('attempts')) is int
        and 0 <= state['attempts'] <= ATTEMPT_LIMIT
        and state['issued_at'] <= state['window_started'] <= state['last_seen'] < state['expires_at']
        and ('expired' not in state or type(state['expired']) is bool)
    )


def new_state(token):
    now = time.time()
    state = {'digest': hashlib.sha256(token.encode('ascii')).hexdigest(),
             'issued_at': now, 'expires_at': now + LIFETIME, 'last_seen': now,
             'window_started': now, 'attempts': 0}
    remember_deadline(state)
    return state


def remember_deadline(state):
    # Initialized in the startup parent, inherited by forked workers. A clock
    # rollback cannot revive a token, even if no HTTP request observed expiry.
    _deadlines.clear()
    _deadlines[state['digest']] = time.monotonic() + min(LIFETIME, max(0, state['expires_at'] - time.time()))


def public_setup_url(config):
    webui = config.get('webui', {})
    if not isinstance(webui, dict):
        raise TypeError('webui must be a mapping')
    value = webui.get('public_base_url')
    if value is None:
        port = webui.get('port', 7767)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('webui.port must be a valid integer port')
        return f"http://127.0.0.1:{port}/setup"
    if not isinstance(value, str) or not value or any(
        char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    ) or any(char in value for char in ('\\', '?', '#')):
        raise ValueError('webui.public_base_url is invalid')
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.netloc.endswith(':') or (port is not None and not 1 <= port <= 65535)
                or '%' in parts.netloc or parts.path not in ('', '/')):
            raise ValueError
        host = parts.hostname
        if parts.netloc.startswith('['):
            if not re.fullmatch(r'\[[^\]]+\](?::[0-9]+)?', parts.netloc):
                raise ValueError
            ipaddress.IPv6Address(host)
        if ':' in host or re.fullmatch(r'[0-9.]+', host):
            ipaddress.ip_address(host)
        elif (len(host) > 253 or host.endswith('..')
              or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                                   for label in host.rstrip('.').split('.'))):
            raise ValueError
        if parts.hostname in ('0.0.0.0', '::'):
            raise ValueError
    except ValueError:
        raise ValueError('webui.public_base_url requires a valid HTTP/HTTPS origin') from None
    return value.rstrip('/') + '/setup'


def load_auth_config(content):
    """Reject ambiguous duplicate authentication keys before provisioning."""
    config = yaml.safe_load(content) or {}
    root = yaml.compose(content)
    if not isinstance(config, dict) or root is not None and not isinstance(root, yaml.MappingNode):
        raise ValueError('Configuration must be a YAML mapping')
    for section in ('admin', 'api'):
        nodes = [value for key, value in root.value if key.value == section] if root else []
        if len(nodes) > 1:
            raise ValueError('Duplicate authentication configuration section')
        if nodes and isinstance(nodes[0], yaml.MappingNode):
            keys = [key.value for key, _ in nodes[0].value]
            if len(keys) != len(set(keys)):
                raise ValueError('Duplicate authentication configuration field')
    return config


def render_admin(content, admin):
    # Replace just the admin mapping; preserve other sections and comments.
    root = yaml.compose(content)
    entry = next(((k, v) for k, v in root.value if k.value == 'admin'), None)
    node = entry[1] if entry else None
    # Replacing anchored/aliased admin mappings could alter other sections
    # or leave dangling aliases. Fail before touching the authoritative file.
    if entry and (node.start_mark.index < entry[0].end_mark.index or any(
            isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
            and entry[0].end_mark.index <= token.start_mark.index < node.end_mark.index
            for token in yaml.scan(content)
    )):
        raise ValueError('Setup requires an explicit administrator mapping without YAML anchors')
    rendered = yaml.safe_dump(admin, sort_keys=False, default_flow_style=True).strip()
    if node is None:
        return content.rstrip() + '\nadmin: ' + rendered + '\n'
    return content[:node.start_mark.index] + rendered + '\n' + content[node.end_mark.index:]


def complete_setup(token, password, confirmation):
    """Return a fixed outcome. Serialize validation, rate limiting and commit."""
    path = get_config_path()
    with config_lock(path):
        with open(path, encoding='utf-8') as source:
            content = source.read()
        config = load_auth_config(content)
        if not is_pending(config) or not valid_state(config['admin']):
            return 'unavailable'
        secret = os.environ.get('WAMF_SECRET_KEY') or config['admin'].get('session_secret')
        api = config.get('api', {})
        api_hash = api.get('token_hash') if isinstance(api, dict) else False
        if (not isinstance(secret, str) or not secret.strip()
                or secret.strip() == '<long random session secret>'
                or not (api_hash is None or isinstance(api_hash, str) and api_hash.strip() in (
                    '', '<generated werkzeug token hash>',
                ))):
            return 'unavailable'
        admin = dict(config['admin'])
        state = dict(admin['setup'])
        now = time.time()
        deadline = _deadlines.get(state['digest'])
        # Missing process deadline means this process did not inherit a native
        # bootstrap. Fail closed instead of reconstructing a longer lifetime.
        if deadline is None:
            return 'unavailable'
        if state.get('expired') is True:
            return 'expired'
        expired = (time.monotonic() >= deadline
                   or now < state['last_seen'] or now >= state['expires_at'])
        if expired:
            state['expired'] = True  # Persist terminal expiry.
            admin['setup'] = state
            atomic_write_config(path, render_admin(content, admin))
            return 'expired'
        if now - state['window_started'] >= ATTEMPT_WINDOW:
            state.update(window_started=now, attempts=0)
        state['last_seen'] = now
        admin['setup'] = state
        token_ok = (isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token)
                    and hmac.compare_digest(hashlib.sha256(token.encode('ascii')).hexdigest(), state['digest']))
        if not token_ok:
            # Only invalid tokens spend the shared abuse allowance. The secret
            # holder can still redeem once; invalid requests never run a KDF.
            if state['attempts'] >= ATTEMPT_LIMIT:
                return 'limited'
            state['attempts'] += 1
            atomic_write_config(path, render_admin(content, admin))
            return 'invalid'
        if (not isinstance(password, str) or not isinstance(confirmation, str)
                or not 8 <= len(password) <= 1024 or password != confirmation):
            atomic_write_config(path, render_admin(content, admin))
            return 'password'
        password_hash = generate_password_hash(password)
        # Hashing can cross the expiration boundary; recheck before replacement.
        now = time.time()
        if now < state['last_seen'] or now >= state['expires_at'] or time.monotonic() >= deadline:
            state['expired'] = True
            atomic_write_config(path, render_admin(content, admin))
            return 'expired'
        completed_admin = dict(admin)
        completed_admin['password_hash'] = password_hash
        completed_admin.pop('setup_pending', None)
        completed_admin.pop('setup', None)

        def check_before_replace():
            now = time.time()
            if now < state['last_seen'] or now >= state['expires_at'] or time.monotonic() >= deadline:
                _deadlines[state['digest']] = float('-inf')
                raise _SetupExpired

        try:
            atomic_write_config(path, render_admin(content, completed_admin), before_replace=check_before_replace)
        except _SetupExpired:
            state['expired'] = True
            atomic_write_config(path, render_admin(content, admin))
            return 'expired'
        return 'success'
